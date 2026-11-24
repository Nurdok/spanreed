"""The blocks that Spanreed itself publishes, for every user."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from spanreed.workflows.blocks import (
    Action,
    Block,
    BlockKind,
    Branch,
    Loop,
    NoOutput,
    NoQuestion,
    Repeat,
    StepContext,
    StopRun,
    Trigger,
    TriggerContext,
)

_LAST_FIRED = "last_fired"


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock of the schedule.
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class Tick:
    """One event of a schedule, in the user's timezone."""

    # YYYY-MM-DD
    date: str
    # HH:MM
    time: str


class Schedule(Trigger[Repeat, Tick]):
    """Fire again and again, a fixed time apart.

    The time of the last event is kept, so a restart does not fire early
    and does not lose its place: a workflow that is due fires at once, and
    one that is not waits for the rest of its interval. A new workflow fires
    at once.
    """

    id = "core.schedule"
    title = "On a schedule"
    Manual = NoQuestion

    async def events(
        self, ctx: TriggerContext, params: Repeat
    ) -> AsyncGenerator[Tick, bool | None]:
        """Yield an event each time the interval has passed."""
        while True:
            last = await self._last_fired(ctx)
            if last is not None:
                wait = (last + params.interval - _now()).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
            fired = _now()
            yield self._tick(ctx, fired)
            # Kept only now, when the run is over: a run that a restart cut
            # short is due again when Spanreed comes back.
            await ctx.state.set(_LAST_FIRED, fired.isoformat())

    async def manual_events(
        self, ctx: TriggerContext, params: Repeat, answer: NoQuestion
    ) -> list[Tick]:
        """Return one event for now. The schedule keeps its own pace."""
        return [self._tick(ctx, _now())]

    @staticmethod
    async def _last_fired(ctx: TriggerContext) -> datetime.datetime | None:
        stored = await ctx.state.get(_LAST_FIRED)
        if stored is None:
            return None
        try:
            return datetime.datetime.fromisoformat(stored)
        except ValueError:
            return None

    @staticmethod
    def _tick(ctx: TriggerContext, instant: datetime.datetime) -> Tick:
        local = instant.astimezone(ctx.user.tzinfo)
        return Tick(date=local.date().isoformat(), time=local.strftime("%H:%M"))


@dataclass
class ContinueIfInput:
    """The input of ``ContinueIf``."""

    condition: bool


class ContinueIf(Action[ContinueIfInput, NoOutput]):
    """End the run here, without failing it, unless a condition holds."""

    id = "core.continue-if"
    title = "Only continue if"
    kind = BlockKind.LOGIC

    async def run(self, ctx: StepContext, inputs: ContinueIfInput) -> NoOutput:
        """Raise ``StopRun`` if the condition is false."""
        if not inputs.condition:
            raise StopRun("The condition is false.")
        return NoOutput()


@dataclass
class IfInput:
    """The input of ``If``."""

    condition: bool


@dataclass
class IfOutput:
    """What ``If`` gives: which way it went."""

    # Whether the condition held.
    yes: bool


class If(Branch[IfInput, IfOutput]):
    """Run one list of steps if a condition holds, and another if not."""

    id = "core.if"
    title = "If"
    first_title = "If yes"
    second_title = "Otherwise"

    def takes_first(self, inputs: IfInput) -> bool:
        """Return whether the condition holds."""
        return bool(inputs.condition)

    async def run(self, ctx: StepContext, inputs: IfInput) -> IfOutput:
        """Return which way the branch goes."""
        return IfOutput(yes=bool(inputs.condition))


@dataclass
class ForEachInput:
    """The input of ``ForEach``."""

    items: list[Any]


@dataclass
class ForEachOutput:
    """What ``ForEach`` gives: inside, the item of the round; after, the count."""

    # The item that the steps inside run for. Empty after the loop.
    item: Any = None
    # The place of the item, from 1. After the loop, 0.
    index: int = 0
    # How many items there are.
    count: int = 0


class ForEach(Loop[ForEachInput, ForEachOutput]):
    """Run the steps inside once for each item of a list."""

    id = "core.for-each"
    title = "For each"
    inside_title = "For each item"
    round_name = "Item"

    def rounds(self, inputs: ForEachInput) -> list[ForEachOutput]:
        """Return each item with its place and the number of items."""
        count = len(inputs.items)
        return [
            ForEachOutput(item=item, index=index, count=count)
            for index, item in enumerate(inputs.items, start=1)
        ]

    async def run(self, ctx: StepContext, inputs: ForEachInput) -> ForEachOutput:
        """Return the number of items, for the steps after the loop."""
        return ForEachOutput(count=len(inputs.items))


@dataclass
class SendMessageInput:
    """The input of ``SendMessage``."""

    message: str


class SendMessage(Action[SendMessageInput, NoOutput]):
    """Send the user a message, on the surfaces that reach them now.

    The message is plain text: it is shown as it is written, whatever
    values were filled into it. A message that comes out empty is not
    sent, so a message that is all inside ``{% if … %}`` is sent only then.
    """

    id = "core.send-message"
    title = "Send message"
    kind = BlockKind.ASK

    async def run(self, ctx: StepContext, inputs: SendMessageInput) -> NoOutput:
        """Send the message, unless there is nothing in it."""
        if inputs.message.strip():
            await ctx.interaction.send_message(inputs.message, parse_html=False)
        return NoOutput()


def builtin_blocks() -> list[Block]:
    """Return the blocks that Spanreed itself publishes."""
    return [Schedule(), ContinueIf(), If(), ForEach(), SendMessage()]
