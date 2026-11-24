"""Helpers for tests of workflows: fake blocks, and an engine without Redis."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.plugin import Plugin
from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.workflows import store
from spanreed.workflows.blocks import (
    Action,
    Block,
    BlockRegistry,
    NoParams,
    NoQuestion,
    PushTrigger,
    StepContext,
    Trigger,
    TriggerContext,
)
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, TriggerSpec, Workflow


@dataclass
class Params:
    """The parameters of the fake triggers."""

    events: list[str] = field(default_factory=list)


@dataclass
class Event:
    """The event of the fake triggers."""

    value: str


class Events(Trigger[Params, Event]):
    """A trigger that yields the events in its parameters, then ends.

    By hand it gives one event, ``by hand``.
    """

    id = "fake.events"
    title = "Fake events"
    Manual: ClassVar[type[Any] | None] = NoQuestion

    async def events(
        self, ctx: TriggerContext, params: Params
    ) -> AsyncGenerator[Event, bool | None]:
        """Yield each event of the parameters."""
        for value in params.events:
            yield Event(value)

    async def manual_events(
        self, ctx: TriggerContext, params: Params, answer: Any
    ) -> list[Event]:
        """Return one event."""
        return [Event("by hand")]


@dataclass
class Range:
    """What ``Ranged`` asks before a manual run."""

    days: Literal["Today", "The last week"]


class Ranged(Events):
    """A trigger that asks for a range before a manual run."""

    id = "fake.ranged"
    title = "Fake ranged"
    Manual = Range

    async def manual_events(
        self, ctx: TriggerContext, params: Params, answer: Any
    ) -> list[Event]:
        """Return one event for today, or seven for the last week."""
        count = 1 if answer.days == "Today" else 7
        return [Event(f"day {day}") for day in range(count)]


class Silent(Events):
    """A trigger that cannot be started by hand."""

    id = "fake.silent"
    title = "Fake silent"
    Manual = None


class Pushed(PushTrigger[NoParams, Event]):
    """A trigger that its plugin feeds. The event of a key holds the key.

    The key ``nothing`` has no event.
    """

    id = "fake.pushed"
    title = "Fake pushed"

    async def event_for(
        self, ctx: TriggerContext, params: NoParams, key: str
    ) -> Event | None:
        """Return an event that holds the key."""
        return None if key == "nothing" else Event(key)


@dataclass
class TextInput:
    """One text."""

    text: str


@dataclass
class TextOutput:
    """One text, and its length."""

    text: str
    length: int


class Record(Action[TextInput, TextOutput]):
    """An action that keeps every text it was given.

    ``failures`` holds what to raise in the next calls, first one first.
    ``None`` in it is a call that works.
    """

    id = "fake.record"
    title = "Fake record"

    def __init__(self) -> None:
        """Create the block with nothing recorded."""
        self.texts: list[str] = []
        self.failures: list[Exception | None] = []

    async def run(self, ctx: StepContext, inputs: TextInput) -> TextOutput:
        """Keep the text, or raise the next failure."""
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        self.texts.append(inputs.text)
        return TextOutput(text=inputs.text, length=len(inputs.text))


def fake_plugin(registered: bool = True) -> MagicMock:
    """Return a stand-in for the plugin that publishes the fake blocks."""
    plugin = MagicMock(spec=Plugin)
    plugin.name.return_value = "Fake"
    plugin.canonical_name.return_value = "fake"
    plugin.is_registered = AsyncMock(return_value=registered)
    return plugin


def registry_of(*blocks: Block, registered: bool = True) -> BlockRegistry:
    """Return a registry of these blocks and of the fake triggers."""
    registry = BlockRegistry()
    plugin = fake_plugin(registered)
    fakes: list[Block] = [Events(), Ranged(), Silent(), Pushed()]
    for block in (*fakes, *blocks):
        registry.register(block, plugin)
    return registry


def workflow_of(
    *steps: Step,
    trigger: str = "fake.events",
    events: list[str] | None = None,
    **fields: Any,
) -> Workflow:
    """Return a workflow of user 4 with these steps."""
    values: dict[str, Any] = {"id": "wf_1", "user_id": 4, "name": "Test workflow"}
    values.update(fields)
    params = {} if trigger == "fake.pushed" else {"events": events or []}
    return Workflow(
        trigger=TriggerSpec(block=trigger, params=params),
        steps=list(steps),
        **values,
    )


@contextlib.contextmanager
def fake_storage() -> Iterator[FakeRedis]:
    """Keep workflows in memory, and load users without Redis."""
    redis = FakeRedis()
    store.reset_queues_for_tests()
    with patch("spanreed.workflows.store.redis_api", new=redis), patch(
        "spanreed.workflows.engine.User.find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        yield redis


async def drain(engine: WorkflowEngine, workflow: Workflow) -> None:
    """Wait until the trigger of a started workflow has no more events."""
    await engine._sources[workflow.id]
