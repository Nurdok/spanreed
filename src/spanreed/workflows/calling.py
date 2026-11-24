"""The blocks with which one workflow runs another.

A workflow that is a part of others has the trigger "When another workflow
runs it". "Run a workflow" runs one by its name, and "Run one workflow"
picks one of several, at random or by asking the user.

The other workflow runs inside the run of the step: in the same
conversation, and the step is over when it is. Its trace is in its own
history. A workflow can also be run this way if its trigger starts a run by
hand without a question, such as a schedule.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Literal

from spanreed.config_form import HELP_KEY
from spanreed.workflows.asking import Command, Priority, give_up_field, priority_field
from spanreed.workflows.blocks import (
    Action,
    Block,
    BlockKind,
    NoParams,
    StepContext,
    TriggerContext,
)
from spanreed.workflows.builtin import Tick

# Indirection so that tests can say which one is picked.
_choice = random.choice

# What a row can hold to say that a workflow is not on offer.
_NO = ("", "no", "false", "off", "0")


class WhenCalled(Command):
    """Start a run when a step of another workflow runs this one.

    The workflow does nothing by itself. It also has a command, which its
    recipe usually hides.
    """

    id = "core.called"
    title = "When another workflow runs it"

    async def events(
        self, ctx: TriggerContext, params: NoParams
    ) -> AsyncGenerator[Tick, bool | None]:
        """Yield nothing: only another workflow, or the user, starts a run."""
        never = asyncio.Event()
        while not await never.wait():
            yield self._tick(ctx)


@dataclass
class CallWorkflowInput:
    """Which workflow to run."""

    workflow: str = field(metadata={HELP_KEY: "The name of one of your workflows"})
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class CallWorkflowOutput:
    """How the run of the other workflow went."""

    # ``finished``, or ``stopped`` if one of its steps ended it early.
    status: str
    finished: bool
    outcome: str


class CallWorkflow(Action[CallWorkflowInput, CallWorkflowOutput]):
    """Run another workflow of the user, and go on when it is over.

    The step fails if the other workflow fails, is off, or does not exist.
    """

    id = "core.call-workflow"
    title = "Run a workflow"
    kind = BlockKind.LOGIC
    # The other workflow may ask.
    converses = True

    async def run(
        self, ctx: StepContext, inputs: CallWorkflowInput
    ) -> CallWorkflowOutput:
        """Run the workflow."""
        called = await ctx.workflows.run(inputs.workflow)
        return CallWorkflowOutput(
            status=called.status,
            finished=called.status == "finished",
            outcome=called.outcome,
        )


@dataclass
class RunOneInput:
    """The workflows to pick from, and how."""

    workflows: dict[str, Any] = field(
        metadata={
            HELP_KEY: "The name of a workflow, and whether it is on offer: "
            "yes, no, or a condition"
        }
    )
    how: Literal["at random", "ask me"] = "at random"
    question: str = field(
        default="Which one?", metadata={HELP_KEY: "Asked if you are the one to pick"}
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class RunOneOutput:
    """Which workflow ran, and how it went."""

    # The name of the workflow that ran. Empty if none was on offer.
    ran: str
    # ``finished`` or ``stopped``. Empty if none ran.
    status: str
    # The names that were on offer.
    on_offer: list[str]


def _is_on(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _NO
    return bool(value)


class RunOne(Action[RunOneInput, RunOneOutput]):
    """Run one of several workflows: one picked at random, or the user's pick.

    A workflow is on offer if its row says so and it can run: it is one of
    the user's, it is on, and nothing is wrong with it. Nothing happens if
    none is on offer.
    """

    id = "core.run-one"
    title = "Run one workflow"
    kind = BlockKind.LOGIC
    converses = True

    async def run(self, ctx: StepContext, inputs: RunOneInput) -> RunOneOutput:
        """Pick a workflow that is on offer, and run it."""
        wanted = [name for name, on in inputs.workflows.items() if _is_on(on)]
        on_offer = await ctx.workflows.on_offer(wanted)
        if not on_offer:
            return RunOneOutput(ran="", status="", on_offer=[])
        if inputs.how == "ask me":
            choice = await ctx.interaction.request_user_choice(
                inputs.question, list(on_offer)
            )
            name = on_offer[choice]
        else:
            name = _choice(on_offer)
        called = await ctx.workflows.run(name)
        return RunOneOutput(ran=called.name, status=called.status, on_offer=on_offer)


def calling_blocks() -> list[Block]:
    """Return the blocks with which one workflow runs another."""
    return [WhenCalled(), CallWorkflow(), RunOne()]
