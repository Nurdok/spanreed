"""The blocks that keep something from one run of a workflow to the next.

A workflow has a memory of its own: JSON values under names. "Remember"
keeps one, "Recall" reads one, "Forget" drops one. A block reaches the
same memory through ``ctx.memory``.

The usual use is to do something once: recall ``asked:{{ book.asin }}``,
go on only if nothing is found, ask, and remember it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from spanreed.config_form import HELP_KEY
from spanreed.workflows.blocks import (
    Action,
    Block,
    BlockKind,
    NoOutput,
    StepContext,
)


@dataclass
class RememberInput:
    """What to keep, and under which name."""

    name: str
    value: Any = field(
        default=True, metadata={HELP_KEY: "Empty: only that it is remembered"}
    )


class Remember(Action[RememberInput, NoOutput]):
    """Keep a value under a name, for the later runs of this workflow."""

    id = "core.remember"
    title = "Remember"
    kind = BlockKind.LOGIC

    async def run(self, ctx: StepContext, inputs: RememberInput) -> NoOutput:
        """Keep the value, in place of what the name held."""
        await ctx.memory.set(
            inputs.name, True if inputs.value is None else inputs.value
        )
        return NoOutput()


@dataclass
class RecallInput:
    """Which name to look up."""

    name: str


@dataclass
class RecallOutput:
    """What the workflow remembers under the name."""

    # Whether anything is kept under the name.
    found: bool
    value: Any = None
    # When it was kept, in UTC. Empty if nothing is.
    at: str = ""
    # How many hours ago that was. 0 if nothing is kept. For doing
    # something at most every so often: ``not last.found or
    # last.age_hours > 72``.
    age_hours: float = 0.0


class Recall(Action[RecallInput, RecallOutput]):
    """Read what this workflow remembers under a name."""

    id = "core.recall"
    title = "Recall"
    kind = BlockKind.LOGIC

    async def run(self, ctx: StepContext, inputs: RecallInput) -> RecallOutput:
        """Return what is kept, or that nothing is."""
        kept = await ctx.memory.get(inputs.name)
        if kept is None:
            return RecallOutput(found=False)
        return RecallOutput(
            found=True, value=kept.value, at=kept.at, age_hours=_hours_since(kept.at)
        )


@dataclass
class ForgetInput:
    """Which name to drop."""

    name: str


@dataclass
class ForgetOutput:
    """Whether there was anything to drop."""

    found: bool


class Forget(Action[ForgetInput, ForgetOutput]):
    """Drop what this workflow remembers under a name."""

    id = "core.forget"
    title = "Forget"
    kind = BlockKind.LOGIC

    async def run(self, ctx: StepContext, inputs: ForgetInput) -> ForgetOutput:
        """Drop it, and return whether there was anything."""
        return ForgetOutput(found=await ctx.memory.forget(inputs.name))


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock.
    return datetime.datetime.now(datetime.timezone.utc)


def _hours_since(at: str) -> float:
    """Return how many hours ago a time that a memory kept was."""
    try:
        moment = datetime.datetime.fromisoformat(at)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return round(max((_now() - moment).total_seconds(), 0.0) / 3600, 2)


def memory_blocks() -> list[Block]:
    """Return the blocks for a workflow's memory."""
    return [Remember(), Recall(), Forget()]
