"""Tests of the triggers for what happens to Spanreed itself."""

import asyncio
import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import dateutil.tz
import pytest

from spanreed.interaction import commands
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows import system
from spanreed.workflows.blocks import BlockRegistry, NoParams, TriggerContext
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.system import (
    ErrorReported,
    Start,
    Started,
    Trouble,
    system_blocks,
)
from spanreed.workflows.testing import (
    Record,
    fake_plugin,
    fake_storage,
    registry_of,
    workflow_of,
)

MORNING = datetime.datetime(2026, 11, 24, 6, 30, tzinfo=datetime.timezone.utc)


def setup_function() -> None:
    commands.reset_registry_for_tests()


def _ctx(timezone: str = "UTC") -> TriggerContext:
    user = mock_user_find_by_id(4)
    user.tzinfo = dateutil.tz.gettz(timezone)
    return TriggerContext(
        user=user, workflow_id="wf_1", state=AsyncMock(), queue=WorkflowQueue("wf_1")
    )


def _engine(record: Record) -> WorkflowEngine:
    return WorkflowEngine(registry_of(record, *system_blocks()))


def _record(text: str) -> Step:
    return Step(id="one", block="fake.record", inputs={"text": text})


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_the_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in system_blocks():
        registry.register(block, fake_plugin())

    assert [block.id for block in registry.all()] == ["core.started", "core.error"]
    # There is nothing to fetch: neither can be started by hand.
    assert Started.Manual is None
    assert ErrorReported.Manual is None


# -- Spanreed started ---------------------------------------------------------


def test_a_start_is_given_in_the_time_of_the_user() -> None:
    event = asyncio.run(
        Started().event_for(_ctx("Asia/Jerusalem"), NoParams(), MORNING.isoformat())
    )

    assert event == Start(date="2026-11-24", time="08:30")


def test_a_key_that_is_no_time_is_no_start() -> None:
    assert asyncio.run(Started().event_for(_ctx(), NoParams(), "soon")) is None


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_is_on_runs_when_spanreed_has_started(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    on = workflow_of(
        _record("Up since {{ trigger.date }} {{ trigger.time }}."),
        trigger="core.started",
    )
    off = workflow_of(
        _record("Never."), trigger="core.started", id="wf_off", enabled=False
    )
    other = workflow_of(_record("Not mine."), trigger="core.error", id="wf_other")

    async def scenario() -> None:
        engine = _engine(record)
        for workflow in (on, off, other):
            await engine.save(workflow)

        await system.say_started([mock_user_find_by_id(4)])
        await _until(lambda: record.texts)

        assert record.texts == ["Up since 2026-11-24 06:30."]
        # A workflow that was off does not hear of the start later.
        assert not await engine.store.queue(off.id).keys()
        assert not await engine.store.queue(other.id).keys()
        await engine.stop()

    with fake_storage(), patch("spanreed.workflows.system._now", lambda: MORNING):
        asyncio.run(scenario())


# -- Spanreed had an error ----------------------------------------------------


def test_an_error_is_given_with_its_text_and_the_time_of_the_user() -> None:
    key = ErrorReported.key("ValueError: boom\n", MORNING)

    event = asyncio.run(
        ErrorReported().event_for(_ctx("Asia/Jerusalem"), NoParams(), key)
    )

    assert event == Trouble(text="ValueError: boom\n", date="2026-11-24", time="08:30")


@pytest.mark.parametrize(
    "key",
    [
        "not json",
        json.dumps(["a list"]),
        json.dumps({"at": MORNING.isoformat()}),
        json.dumps({"at": "soon", "text": "ValueError: boom"}),
        json.dumps({"text": "ValueError: boom"}),
    ],
)
def test_a_key_that_holds_no_error_is_no_event(key: str) -> None:
    assert asyncio.run(ErrorReported().event_for(_ctx(), NoParams(), key)) is None


def _errors(*texts: str | Exception) -> Any:
    """Patch the list of errors: what ``next_report`` gives, and then the end."""
    left = list(texts)

    async def next_report() -> str:
        if not left:
            # Not an ``Exception``: the loop must not take it for a failure.
            raise asyncio.CancelledError()
        text = left.pop(0)
        if isinstance(text, Exception):
            raise text
        return text

    return patch("spanreed.workflows.system.errors.next_report", new=next_report)


async def _pass_on(users: list[Any]) -> None:
    async def get_users() -> list[Any]:
        return users

    with pytest.raises(asyncio.CancelledError):
        await system.pass_errors_on(get_users)


@patch_user_interaction("spanreed.workflows.engine")
def test_each_error_is_a_run_of_the_workflows_that_want_it(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(_record("{{ trigger.text }}"), trigger="core.error")
    of_another = workflow_of(
        _record("9: {{ trigger.text }}"), trigger="core.error", id="wf_9", user_id=9
    )
    drop = AsyncMock()

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        await engine.save(of_another)

        await _pass_on([mock_user_find_by_id(4), mock_user_find_by_id(9)])
        await _until(lambda: len(record.texts) == 4)

        # The errors of Spanreed as a whole go to each of them.
        assert sorted(record.texts) == [
            "9: KeyError: 'second'",
            "9: ValueError: first",
            "KeyError: 'second'",
            "ValueError: first",
        ]
        await engine.stop()

    with fake_storage(), _errors("ValueError: first", "KeyError: 'second'"), patch(
        "spanreed.workflows.system.errors.drop_waiting", new=drop
    ):
        asyncio.run(scenario())

    # What waited behind each error was dropped.
    assert drop.await_count == 2


@patch_user_interaction("spanreed.workflows.engine")
def test_an_error_that_no_workflow_wants_is_dropped(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("started"), trigger="core.started")

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)

        await _pass_on([mock_user_find_by_id(4)])

        assert not await engine.store.queue(workflow.id).keys()
        assert not record.texts
        await engine.stop()

    with fake_storage(), _errors("ValueError: boom"), patch(
        "spanreed.workflows.system.errors.drop_waiting", new=AsyncMock()
    ):
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_passing_on_goes_on_after_it_failed_itself(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("{{ trigger.text }}"), trigger="core.error")
    sleep = AsyncMock()

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)

        await _pass_on([mock_user_find_by_id(4)])
        await _until(lambda: record.texts)

        assert record.texts == ["ValueError: after"]
        await engine.stop()

    with fake_storage(), _errors(
        ConnectionError("Redis is away"), "ValueError: after"
    ), patch("spanreed.workflows.system.errors.drop_waiting", new=AsyncMock()), patch(
        "spanreed.workflows.system._sleep", new=sleep
    ):
        asyncio.run(scenario())

    sleep.assert_awaited_once_with(system.RETRY_S)
