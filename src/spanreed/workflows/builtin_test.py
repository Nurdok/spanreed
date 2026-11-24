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
    ForEach,
    ForEachInput,
    ForEachOutput,
    If,
    IfInput,
    AtTime,
    IfOutput,
    RandomSchedule,
    RandomTimes,
    Schedule,
    SendMessage,
    SendMessageInput,
    Tick,
    TimeOfDay,
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
        "core.at-time",
        "core.random-schedule",
        "core.continue-if",
        "core.if",
        "core.for-each",
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


def test_if_goes_the_first_way_when_its_condition_holds() -> None:
    assert If().takes_first(IfInput(condition=True))
    assert not If().takes_first(IfInput(condition=False))
    assert asyncio.run(If().run(_step_context(), IfInput(condition=True))) == IfOutput(
        yes=True
    )
    assert If().lanes() == [("steps", "If yes"), ("otherwise", "Otherwise")]
    assert ForEach().lanes() == [("steps", "For each item")]
    assert SendMessage().lanes() == []


def test_for_each_gives_each_item_with_its_place_and_then_the_count() -> None:
    inputs = ForEachInput(items=["a", "b"])

    assert ForEach().rounds(inputs) == [
        ForEachOutput(item="a", index=1, count=2),
        ForEachOutput(item="b", index=2, count=2),
    ]
    assert asyncio.run(ForEach().run(_step_context(), inputs)) == ForEachOutput(count=2)
    assert ForEach().rounds(ForEachInput(items=[])) == []


def test_continue_if_ends_the_run_when_the_condition_is_false() -> None:
    block = ContinueIf()
    assert asyncio.run(block.run(_step_context(), ContinueIfInput(True))) == NoOutput()
    with pytest.raises(StopRun):
        asyncio.run(block.run(_step_context(), ContinueIfInput(False)))


@patch_user_interaction("spanreed.workflows.blocks")
def test_send_message_sends_plain_text(mock_bot: AsyncMock) -> None:
    message = "Updated 2 <overdue> tasks & more."

    asyncio.run(SendMessage().run(_step_context(), SendMessageInput(message)))

    mock_bot.send_message.assert_awaited_once_with(
        message, parse_html=False, parse_markdown=False
    )


@patch_user_interaction("spanreed.workflows.blocks")
def test_a_message_that_comes_out_empty_is_not_sent(mock_bot: AsyncMock) -> None:
    for message in ("", "  \n "):
        asyncio.run(SendMessage().run(_step_context(), SendMessageInput(message)))

    mock_bot.send_message.assert_not_called()


# -- at random times ---------------------------------------------------------


class _Clock:
    """A clock that the waits of the random schedule move forward."""

    def __init__(self, now: datetime.datetime, apart: list[float]) -> None:
        self.now = now
        # What "at random" gives, in the unit of the schedule, in order.
        self.apart = list(apart)
        self.waits: list[float] = []

    def time(self) -> datetime.datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += datetime.timedelta(seconds=seconds)

    def uniform(self, low: float, high: float) -> float:
        assert low <= self.apart[0] <= high
        return self.apart.pop(0)


async def _random_events(
    state: _State, params: RandomTimes, count: int, clock: _Clock
) -> list[Tick]:
    with patch("spanreed.workflows.builtin._now", clock.time), patch(
        "spanreed.workflows.builtin._sleep", clock.sleep
    ), patch("spanreed.workflows.builtin._uniform", clock.uniform):
        source = RandomSchedule().events(_trigger_context(state), params)
        events = []
        try:
            event = await anext(source)
            events.append(event)
            while len(events) < count:
                events.append(await source.asend(True))
        finally:
            await source.aclose()
    return events


def test_random_times_are_a_different_wait_apart_each_time() -> None:
    clock = _Clock(NOON, [1.5, 1.0, 2.0])

    events = asyncio.run(_random_events(_State(), RandomTimes(1, 2), 3, clock))

    # A new workflow fires at once.
    assert [event.time for event in events] == ["12:00", "13:30", "14:30"]
    assert clock.waits == [1.5 * 3600, 3600]


def test_random_times_pass_over_the_hours_that_are_not_allowed() -> None:
    evening = NOON.replace(hour=21, minute=30)
    clock = _Clock(evening, [1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 1.0])
    params = RandomTimes(1, 2, only_from="08:00", only_until="22:00")

    events = asyncio.run(_random_events(_State(), params, 3, clock))

    # 21:30 fires. 22:30, 00:30, 02:30, 04:30 and 06:30 are passed over.
    assert [(event.date, event.time) for event in events] == [
        ("2026-05-09", "21:30"),
        ("2026-05-10", "08:30"),
        ("2026-05-10", "09:30"),
    ]


def test_random_times_keep_when_the_next_one_is_due_over_a_restart() -> None:
    state = _State()
    clock = _Clock(NOON, [2.0, 1.0])
    asyncio.run(_random_events(state, RandomTimes(1, 2), 2, clock))
    # The first run is over: the second is due at 14:00, and is still
    # running, so nothing later is kept.
    assert state.values == {"next_at": "2026-05-09T14:00:00+00:00"}

    # A restart at 12:20 waits for the rest, and does not fire early.
    restarted = _Clock(NOON.replace(minute=20), [1.0])
    (event,) = asyncio.run(_random_events(state, RandomTimes(1, 2), 1, restarted))
    assert restarted.waits == [100 * 60]
    assert event.time == "14:00"


def test_random_times_by_hand_fire_now_whatever_the_hour() -> None:
    night = NOON.replace(hour=3)
    params = RandomTimes(only_from="08:00", only_until="22:00")

    with patch("spanreed.workflows.builtin._now", return_value=night):
        (event,) = asyncio.run(
            RandomSchedule().manual_events(
                _trigger_context(_State()), params, NoQuestion()
            )
        )

    assert event == Tick(date="2026-05-09", time="03:00")
    assert RandomSchedule.Manual is NoQuestion


def test_the_hours_of_random_times() -> None:
    day = RandomTimes(only_from="08:00", only_until="22:00")
    assert [day.allows(NOON.replace(hour=hour)) for hour in (7, 8, 21, 22)] == [
        False,
        True,
        True,
        False,
    ]
    # Over midnight.
    night = RandomTimes(only_from="22:00", only_until="06:00")
    assert [night.allows(NOON.replace(hour=hour)) for hour in (21, 22, 3, 6)] == [
        False,
        True,
        True,
        False,
    ]
    assert RandomTimes().allows(NOON.replace(hour=3)) is True

    for wrong, why in (
        ({"at_least": 0}, "1 or more"),
        ({"at_least": 3, "at_most": 2}, "At most"),
        ({"only_from": "08:00"}, "both times"),
        ({"only_from": "8 am", "only_until": "22:00"}, "not a time of day"),
    ):
        with pytest.raises(ValueError, match=why):
            RandomTimes(**wrong)
    assert from_data(RandomTimes, {"only_from": "08:00", "only_until": "22:00"}) == day


# -- at a time of day --------------------------------------------------------


async def _timed_events(
    state: _State, params: TimeOfDay, count: int, clock: _Clock
) -> list[Tick]:
    with patch("spanreed.workflows.builtin._now", clock.time), patch(
        "spanreed.workflows.builtin._sleep", clock.sleep
    ):
        source = AtTime().events(_trigger_context(state), params)
        events = []
        try:
            event = await anext(source)
            events.append(event)
            while len(events) < count:
                events.append(await source.asend(True))
        finally:
            await source.aclose()
    return events


def test_a_time_of_day_comes_up_every_day() -> None:
    # NOON is a Saturday.
    clock = _Clock(NOON, [])

    events = asyncio.run(_timed_events(_State(), TimeOfDay("18:30"), 3, clock))

    # A new workflow waits for its first time.
    assert events == [
        Tick(date="2026-05-09", time="18:30"),
        Tick(date="2026-05-10", time="18:30"),
        Tick(date="2026-05-11", time="18:30"),
    ]
    assert clock.waits == [6.5 * 3600, 24 * 3600, 24 * 3600]


def test_a_time_of_day_on_one_day_of_the_week() -> None:
    clock = _Clock(NOON, [])
    tuesdays = TimeOfDay("09:00", on="Tuesday")

    events = asyncio.run(_timed_events(_State(), tuesdays, 2, clock))

    assert [event.date for event in events] == ["2026-05-12", "2026-05-19"]
    # On the day itself, the time is still to come or it is next week.
    saturdays = TimeOfDay("12:00", on="Saturday")
    assert saturdays.next_after(
        NOON, datetime.timezone.utc
    ) == NOON + datetime.timedelta(days=7)
    early = NOON - datetime.timedelta(minutes=1)
    assert saturdays.next_after(early, datetime.timezone.utc) == NOON


def test_a_time_of_day_is_in_its_own_timezone() -> None:
    clock = _Clock(NOON, [])
    params = TimeOfDay("18:00", timezone="Asia/Tokyo")

    (event,) = asyncio.run(_timed_events(_State(), params, 1, clock))

    # 18:00 in Tokyo is 09:00 UTC: that has passed today, so tomorrow.
    assert clock.waits == [21 * 3600]
    assert event == Tick(date="2026-05-10", time="18:00")
    with pytest.raises(ValueError, match="not a timezone"):
        TimeOfDay("18:00", timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="not a time of day"):
        TimeOfDay("6 pm")


def test_a_time_that_came_up_during_a_restart_still_fires() -> None:
    state = _State()
    asyncio.run(_timed_events(state, TimeOfDay("18:30"), 1, _Clock(NOON, [])))
    # Taken, but its run is not over: nothing later is kept.
    assert state.values["next_at"] == "2026-05-09T18:30:00+00:00"

    # Back at 19:00: half an hour late, it fires at once, for 18:30.
    late = _Clock(NOON.replace(hour=19), [])
    first, second = asyncio.run(_timed_events(state, TimeOfDay("18:30"), 2, late))
    assert (first.time, late.waits[:1]) == ("18:30", [23.5 * 3600])
    assert second.date == "2026-05-10"

    # Back a day later: that one is passed over.
    state.values["next_at"] = "2026-05-09T18:30:00+00:00"
    much_later = _Clock(NOON + datetime.timedelta(days=1, hours=7), [])
    (event,) = asyncio.run(_timed_events(state, TimeOfDay("18:30"), 1, much_later))
    assert event.date == "2026-05-11"


def test_a_changed_time_of_day_does_not_fire_at_the_old_one() -> None:
    state = _State()
    asyncio.run(_timed_events(state, TimeOfDay("18:30"), 1, _Clock(NOON, [])))

    clock = _Clock(NOON, [])
    (event,) = asyncio.run(_timed_events(state, TimeOfDay("14:00"), 1, clock))

    assert event.time == "14:00"
    assert clock.waits == [2 * 3600]


def test_a_time_of_day_by_hand_is_now() -> None:
    with patch("spanreed.workflows.builtin._now", return_value=NOON):
        (event,) = asyncio.run(
            AtTime().manual_events(
                _trigger_context(_State()), TimeOfDay("18:30"), NoQuestion()
            )
        )

    assert event == Tick(date="2026-05-09", time="12:00")
