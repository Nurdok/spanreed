import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.blocks import (
    NoOutput,
    NoQuestion,
    Repeat,
    RunInfo,
    StepContext,
    StopRun,
    TriggerContext,
)
from spanreed.workflows.builtin import (
    ContinueIf,
    ContinueIfInput,
    Schedule,
    SendMessage,
    SendMessageInput,
    Tick,
    builtin_blocks,
)
from spanreed.workflows.testing import fake_plugin
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.values import from_data

NOON = datetime.datetime(2026, 5, 9, 12, 0, tzinfo=datetime.timezone.utc)


class _State:
    """A trigger's state, kept in a dict."""

    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


def _trigger_context(state: _State) -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=state,
        queue=WorkflowQueue("wf_1"),
    )


def _step_context() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="r1", manual=False),
    )


async def _first_events(state: _State, count: int, clock: list[Any]) -> list[Tick]:
    """Take ``count`` events from a schedule of every 4 hours.

    The clock moves on by the time the schedule sleeps.
    """

    async def sleep(seconds: float) -> None:
        clock.append(seconds)
        clock[0] += datetime.timedelta(seconds=seconds)

    params = Repeat(every=4, unit="hours")
    events = []
    with patch("spanreed.workflows.builtin._now", new=lambda: clock[0]), patch(
        "spanreed.workflows.builtin.asyncio.sleep", new=sleep
    ):
        async for event in Schedule().events(_trigger_context(state), params):
            events.append(event)
            if len(events) == count:
                break
    return events


def test_the_builtin_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in builtin_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == [
        "core.schedule",
        "core.continue-if",
        "core.send-message",
    ]


def test_a_new_schedule_fires_at_once_and_then_every_interval() -> None:
    state = _State()
    clock: list[Any] = [NOON]

    events = asyncio.run(_first_events(state, 3, clock))

    assert events == [
        Tick(date="2026-05-09", time="12:00"),
        Tick(date="2026-05-09", time="16:00"),
        Tick(date="2026-05-09", time="20:00"),
    ]
    # No wait before the first event, four hours before each of the others.
    assert clock[1:] == [4 * 3600, 4 * 3600]


def test_a_schedule_keeps_its_place_over_a_restart() -> None:
    # Fired an hour before the restart: three hours of the four are left.
    an_hour_ago = (NOON - datetime.timedelta(hours=1)).isoformat()
    clock: list[Any] = [NOON]

    (event,) = asyncio.run(_first_events(_State(last_fired=an_hour_ago), 1, clock))

    assert clock[1:] == [3 * 3600]
    assert event == Tick(date="2026-05-09", time="15:00")


def test_a_schedule_that_is_due_after_a_restart_fires_at_once() -> None:
    long_ago = (NOON - datetime.timedelta(hours=9)).isoformat()
    clock: list[Any] = [NOON]

    (event,) = asyncio.run(_first_events(_State(last_fired=long_ago), 1, clock))

    assert clock[1:] == []
    assert event.time == "12:00"


def test_the_time_of_an_event_is_kept_when_its_run_is_over() -> None:
    state = _State()
    clock: list[Any] = [NOON]

    # One event taken: its run is still going, so nothing is kept yet. A
    # restart now makes the schedule due at once.
    asyncio.run(_first_events(state, 1, clock))
    assert state.values == {}

    # Asking for the next event says that the run is over.
    asyncio.run(_first_events(state, 2, clock))
    assert state.values == {"last_fired": NOON.isoformat()}


def test_an_event_is_in_the_timezone_of_the_user() -> None:
    user = mock_user_find_by_id(4)
    user.tzinfo = datetime.timezone(datetime.timedelta(hours=13))
    ctx = TriggerContext(
        user=user, workflow_id="wf_1", state=_State(), queue=WorkflowQueue("wf_1")
    )

    with patch("spanreed.workflows.builtin._now", new=lambda: NOON):
        events = asyncio.run(Schedule().manual_events(ctx, Repeat(), NoQuestion()))

    assert events == [Tick(date="2026-05-10", time="01:00")]


def test_a_schedule_must_wait_between_events() -> None:
    assert from_data(Repeat, {"every": 4, "unit": "hours"}).interval == (
        datetime.timedelta(hours=4)
    )
    with pytest.raises(ValueError):
        from_data(Repeat, {"every": 0, "unit": "hours"})
    with pytest.raises(ValueError):
        from_data(Repeat, {"every": 4, "unit": "fortnights"})


def test_continue_if_ends_the_run_when_the_condition_is_false() -> None:
    block = ContinueIf()
    assert asyncio.run(block.run(_step_context(), ContinueIfInput(True))) == NoOutput()
    with pytest.raises(StopRun):
        asyncio.run(block.run(_step_context(), ContinueIfInput(False)))


@patch_user_interaction("spanreed.workflows.builtin")
def test_send_message_sends_plain_text(mock_bot: AsyncMock) -> None:
    message = "Updated 2 <overdue> tasks & more."

    asyncio.run(SendMessage().run(_step_context(), SendMessageInput(message)))

    mock_bot.send_message.assert_awaited_once_with(message, parse_html=False)
