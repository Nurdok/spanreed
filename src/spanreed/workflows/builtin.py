"""The blocks that Spanreed itself publishes, for every user."""

from __future__ import annotations

import asyncio
import datetime
import random
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Literal

import dateutil.tz

from spanreed.config_form import HELP_KEY, described

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
_NEXT_AT = "next_at"

# Indirections so that tests can skip the waits and say how long one is.
_sleep = asyncio.sleep
_uniform = random.uniform

_UNIT_SECONDS = {"minutes": 60.0, "hours": 3600.0, "days": 86400.0}


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock of the schedule.
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class Tick:
    """One event of a schedule, in the user's timezone."""

    date: str = described(
        "The day the run started on, as 2026-05-09, in your timezone",
    )
    time: str = described("The time it started at, as 18:30")


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

    def same_event(self, kept: dict[str, Any], event: Tick) -> bool:
        """Return that any event is the one a run was cut short for.

        The time of an event is the moment it is made at, and when the
        next one is due is kept only once a run is over. So the first
        event after a restart stands for the one that the restart cut
        short, at whatever time it comes.
        """
        return True

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


def _time_of_day(text: str) -> datetime.time:
    """Return a time written as HH:MM. Raise ``ValueError`` if it is not one."""
    try:
        return datetime.time.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"“{text}” is not a time of day, such as 08:00.") from None


@dataclass
class RandomTimes:
    """How far apart a trigger fires, give or take, and in which hours."""

    at_least: int = 1
    at_most: int = 2
    unit: Literal["minutes", "hours", "days"] = "hours"
    only_from: str = field(
        default="",
        metadata={HELP_KEY: "A time of day, such as 08:00. Empty: at any time"},
    )
    only_until: str = field(
        default="",
        metadata={HELP_KEY: "A time of day, such as 22:00. Empty: at any time"},
    )

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for waits and hours that mean nothing."""
        if self.at_least < 1:
            raise ValueError("A trigger fires every 1 or more.")
        if self.at_most < self.at_least:
            raise ValueError("“At most” cannot be less than “At least”.")
        if bool(self.only_from.strip()) != bool(self.only_until.strip()):
            raise ValueError("Give both times of day, or neither.")
        if self.only_from.strip():
            _time_of_day(self.only_from)
            _time_of_day(self.only_until)

    def allows(self, local: datetime.datetime) -> bool:
        """Return whether a moment, in the user's timezone, is in the hours."""
        if not self.only_from.strip():
            return True
        start, end = _time_of_day(self.only_from), _time_of_day(self.only_until)
        now = local.time()
        if start <= end:
            return start <= now < end
        # Over midnight: from 22:00 until 06:00.
        return now >= start or now < end


class RandomSchedule(Trigger[RandomTimes, Tick]):
    """Fire again and again, a different time apart each time.

    The wait before the next event is picked at random between the two
    numbers. A moment outside the hours of the day is passed over: the
    wait is over, nothing fires, and the next wait starts.

    When the next event is due is kept, so a restart does not fire early.
    A new workflow fires at once, if the hour allows.
    """

    id = "core.random-schedule"
    title = "At random times"
    Manual = NoQuestion

    async def events(
        self, ctx: TriggerContext, params: RandomTimes
    ) -> AsyncGenerator[Tick, bool | None]:
        """Yield an event after each wait that ends in the hours."""
        while True:
            due = await self._next_at(ctx)
            if due is not None:
                wait = (due - _now()).total_seconds()
                if wait > 0:
                    await _sleep(wait)
            fired = _now()
            if params.allows(fired.astimezone(ctx.user.tzinfo)):
                yield Schedule._tick(ctx, fired)
            # Kept only now, when the run is over: a run that a restart cut
            # short is due again when Spanreed comes back.
            apart = (
                _uniform(params.at_least, params.at_most) * _UNIT_SECONDS[params.unit]
            )
            await ctx.state.set(
                _NEXT_AT, (_now() + datetime.timedelta(seconds=apart)).isoformat()
            )

    def same_event(self, kept: dict[str, Any], event: Tick) -> bool:
        """Return that any event is the one a run was cut short for.

        The time of an event is the moment it is made at, and when the
        next one is due is kept only once a run is over. So the first
        event after a restart stands for the one that the restart cut
        short, at whatever time it comes.
        """
        return True

    async def manual_events(
        self, ctx: TriggerContext, params: RandomTimes, answer: NoQuestion
    ) -> list[Tick]:
        """Return one event for now, whatever the hour. The schedule keeps its pace."""
        return [Schedule._tick(ctx, _now())]

    @staticmethod
    async def _next_at(ctx: TriggerContext) -> datetime.datetime | None:
        stored = await ctx.state.get(_NEXT_AT)
        if stored is None:
            return None
        try:
            return datetime.datetime.fromisoformat(stored)
        except ValueError:
            return None


WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
# An event that Spanreed was not running for still fires if it is found
# within this time. After that it is passed over.
LATE = datetime.timedelta(hours=12)
_NEXT_FOR = "next_for"


@dataclass
class TimeOfDay:
    """A time of day, on every day or on one day of the week."""

    time: str = field(
        default="09:00", metadata={HELP_KEY: "A time of day, such as 18:30"}
    )
    on: Literal[
        "every day",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ] = "every day"
    timezone: str = field(
        default="",
        metadata={
            HELP_KEY: "Such as Europe/Paris. Empty: the timezone of your profile"
        },
    )

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for a time or a timezone that is not one."""
        _time_of_day(self.time)
        if self.timezone.strip() and dateutil.tz.gettz(self.timezone.strip()) is None:
            raise ValueError(f"“{self.timezone}” is not a timezone.")

    def tzinfo(self, default: datetime.tzinfo | None) -> datetime.tzinfo:
        """Return the timezone the time is in: its own, or ``default``."""
        named = (
            dateutil.tz.gettz(self.timezone.strip()) if self.timezone.strip() else None
        )
        return named or default or datetime.timezone.utc

    def next_after(
        self, moment: datetime.datetime, tz: datetime.tzinfo
    ) -> datetime.datetime:
        """Return the first time this comes up after a moment."""
        at = _time_of_day(self.time)
        local = moment.astimezone(tz)
        for days in range(9):
            day = local.date() + datetime.timedelta(days=days)
            candidate = datetime.datetime.combine(day, at, tzinfo=tz)
            on_day = self.on == "every day" or WEEKDAYS[day.weekday()] == self.on
            if on_day and candidate > local:
                return candidate
        raise RuntimeError("No next time was found.")  # pragma: no cover

    @property
    def signature(self) -> str:
        """Return a text that changes when the time, the day or the zone does."""
        return f"{self.time}|{self.on}|{self.timezone}"


class AtTime(Trigger[TimeOfDay, Tick]):
    """Fire at a time of day, on every day or on one day of the week.

    When the next event is due is kept, so that a restart does not lose
    it: an event that came up while Spanreed was not running fires when it
    is back, if that is within half a day. A new workflow waits for its
    first time.

    The date and the time of an event are those it was due at, in the
    timezone of the trigger.
    """

    id = "core.at-time"
    title = "At a time of day"
    Manual = NoQuestion

    async def events(
        self, ctx: TriggerContext, params: TimeOfDay
    ) -> AsyncGenerator[Tick, bool | None]:
        """Yield an event each time the time of day comes up."""
        tz = params.tzinfo(ctx.user.tzinfo)
        while True:
            due = await self._due(ctx, params)
            if due is None or due < _now() - LATE:
                due = params.next_after(_now(), tz)
                await self._keep(ctx, params, due)
            wait = (due - _now()).total_seconds()
            if wait > 0:
                await _sleep(wait)
            yield self._tick_at(due.astimezone(tz))
            # Kept only now, when the run is over: a run that a restart cut
            # short is due again when Spanreed comes back.
            await self._keep(ctx, params, params.next_after(due, tz))

    async def manual_events(
        self, ctx: TriggerContext, params: TimeOfDay, answer: NoQuestion
    ) -> list[Tick]:
        """Return one event for now. The trigger keeps its own times."""
        return [self._tick_at(_now().astimezone(params.tzinfo(ctx.user.tzinfo)))]

    @staticmethod
    def _tick_at(local: datetime.datetime) -> Tick:
        return Tick(date=local.date().isoformat(), time=local.strftime("%H:%M"))

    @staticmethod
    async def _due(ctx: TriggerContext, params: TimeOfDay) -> datetime.datetime | None:
        """Return when the next event is due, if that was kept for these times."""
        if await ctx.state.get(_NEXT_FOR) != params.signature:
            return None
        stored = await ctx.state.get(_NEXT_AT)
        try:
            return datetime.datetime.fromisoformat(stored) if stored else None
        except ValueError:
            return None

    @staticmethod
    async def _keep(
        ctx: TriggerContext, params: TimeOfDay, due: datetime.datetime
    ) -> None:
        await ctx.state.set(_NEXT_AT, due.isoformat())
        await ctx.state.set(_NEXT_FOR, params.signature)


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

    yes: bool = described("Whether the condition held")


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

    item: Any = described(
        "The item that the steps inside run for. Nothing after the loop",
        default=None,
    )
    index: int = described(
        "The place of the item in the list, from 1. 0 after the loop",
        default=0,
    )
    count: int = described(
        "How many items the list has",
        default=0,
    )


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


@dataclass
class NotifyInput:
    """The input of ``Notify``."""

    message: str
    markdown: bool = field(
        default=False,
        metadata={
            HELP_KEY: "Yes: the message is read as Markdown, so that what is "
            "between two lines of ``` is shown as code. No: it is shown as "
            "it is written"
        },
    )


class Notify(Action[NotifyInput, NoOutput]):
    """Leave the user a notification, on every surface they have.

    A message goes to where the user is now, and is lost if that is
    nowhere. A notification waits: a surface that cannot show it now keeps
    it until it can. It is for what the user must not miss and need not
    see at once.

    As for a message, one that comes out empty is not left.
    """

    id = "core.notify"
    title = "Notify me"
    kind = BlockKind.ASK

    async def run(self, ctx: StepContext, inputs: NotifyInput) -> NoOutput:
        """Leave the notification, unless there is nothing in it."""
        if inputs.message.strip():
            await ctx.interaction.notify(
                inputs.message, parse_html=False, parse_markdown=inputs.markdown
            )
        return NoOutput()


def builtin_blocks() -> list[Block]:
    """Return the blocks that Spanreed itself publishes."""
    return [
        Schedule(),
        AtTime(),
        RandomSchedule(),
        ContinueIf(),
        If(),
        ForEach(),
        SendMessage(),
        Notify(),
    ]
