import asyncio
import datetime
import json
from typing import Any
from unittest.mock import patch

import pytest

from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.blocks import Repeat, TriggerContext, digest
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.testing import Event, NoMorePolls, Polled
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


def _context(state: _State) -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=state,
        queue=WorkflowQueue("wf_1"),
    )


def _poll(
    trigger: Polled,
    state: _State,
    *,
    failures: tuple[str, ...] = (),
    clock: list[Any] | None = None,
) -> list[str]:
    """Run the trigger until its polls run out. Return what it yielded.

    Each value in ``failures`` is one run for that value that fails; every
    other run works. The clock starts at ``clock[0]`` and moves on by the
    time the trigger waits; each wait is added to ``clock``.
    """
    clock = clock if clock is not None else [NOON]
    to_fail = list(failures)

    async def sleep(seconds: float) -> None:
        clock.append(seconds)
        clock[0] += datetime.timedelta(seconds=seconds)

    async def take() -> list[str]:
        yielded = []
        events = trigger.events(_context(state), Repeat(every=1, unit="hours"))
        try:
            event = await anext(events)
            while True:
                yielded.append(event.value)
                fails = event.value in to_fail
                if fails:
                    to_fail.remove(event.value)
                event = await events.asend(not fails)
        except NoMorePolls:
            return yielded

    with (
        patch("spanreed.workflows.blocks._now", new=lambda: clock[0]),
        patch("spanreed.workflows.blocks._sleep", new=sleep),
    ):
        return asyncio.run(take())


def test_repeat_must_wait_between_two_times() -> None:
    assert from_data(Repeat, {"every": 4, "unit": "hours"}).interval == (
        datetime.timedelta(hours=4)
    )
    assert Repeat().interval == datetime.timedelta(hours=1)
    with pytest.raises(ValueError):
        from_data(Repeat, {"every": 0, "unit": "hours"})
    with pytest.raises(ValueError):
        from_data(Repeat, {"every": 4, "unit": "fortnights"})


# -- triggers that poll --------------------------------------------------------


def test_a_poll_gives_what_is_new_since_the_polls_before_it() -> None:
    trigger = Polled(["a", "b"], ["a", "b", "c"], ["c"])

    assert _poll(trigger, _State()) == ["a", "b", "c"]


def test_a_thing_runs_again_when_its_version_changes() -> None:
    trigger = Polled(["day@1"], ["day@1"], ["day@2"], ["day@2"])

    assert _poll(trigger, _State()) == ["day@1", "day@2"]


def test_what_ran_is_remembered_over_a_restart() -> None:
    state = _State()

    assert _poll(Polled(["a", "b"]), state) == ["a", "b"]
    assert _poll(Polled(["a", "b", "c"]), state) == ["c"]


def test_a_thing_whose_run_failed_is_given_again_by_the_next_poll() -> None:
    trigger = Polled(["a", "b"], ["a", "b"], ["a", "b"])
    state = _State()

    # "a" fails in the first two polls. "b" is not held up by it.
    assert _poll(trigger, state, failures=("a", "a")) == ["a", "b", "a", "a"]
    assert json.loads(state.values["seen"]) == {"b": "", "a": ""}


def test_the_cursor_moves_on_when_every_run_of_a_poll_worked() -> None:
    trigger = Polled(["a"], ["b", "c"], ["b"], [])
    state = _State()

    # The run for "b" fails in the second poll.
    assert _poll(trigger, state, failures=("b",)) == ["a", "b", "c", "b"]

    # So the second poll kept the cursor of the first, and the third poll
    # started where the second did.
    assert trigger.cursors == [None, "after poll 1", "after poll 1", "after poll 3"]
    assert state.values["cursor"] == "after poll 4"


def test_the_first_poll_is_at_once_and_the_next_an_interval_later() -> None:
    clock: list[Any] = [NOON]

    _poll(Polled(["a"], [], []), _State(), clock=clock)

    # No wait before the first poll. One hour before each of the others,
    # and before the poll that the test ends on.
    assert clock[1:] == [3600, 3600, 3600]


def test_polling_keeps_its_pace_over_a_restart() -> None:
    # Polled 20 minutes before the restart: 40 minutes of the hour are left.
    polled_at = (NOON - datetime.timedelta(minutes=20)).isoformat()
    clock: list[Any] = [NOON]

    _poll(Polled(["a"]), _State(polled_at=polled_at), clock=clock)

    assert clock[1] == 40 * 60


def test_a_poll_that_is_due_after_a_restart_happens_at_once() -> None:
    polled_at = (NOON - datetime.timedelta(hours=5)).isoformat()
    clock: list[Any] = [NOON]

    assert _poll(Polled(["a"]), _State(polled_at=polled_at), clock=clock) == ["a"]
    # The only wait is the one before the poll that the test ends on.
    assert clock[1:] == [3600]


def test_the_time_of_a_poll_is_kept_when_its_runs_are_over() -> None:
    state = _State()
    trigger = Polled(["a"])

    async def take_one() -> None:
        events = trigger.events(_context(state), Repeat())
        await anext(events)
        # The run for "a" is still going: a restart now polls again.
        assert "polled_at" not in state.values
        await events.aclose()

    asyncio.run(take_one())


def test_only_the_last_keys_that_ran_are_remembered() -> None:
    class Forgetful(Polled):
        remembers = 2

    state = _State()

    _poll(Forgetful(["a", "b"], ["a", "c"]), state)

    # "a" was not new in the second poll, so it is still the oldest.
    assert list(json.loads(state.values["seen"])) == ["b", "c"]


def test_state_that_cannot_be_read_starts_over() -> None:
    state = _State(seen="not json", polled_at="not a time")

    assert _poll(Polled(["a"]), state) == ["a"]
    assert json.loads(state.values["seen"]) == {"a": ""}


def test_a_digest_changes_with_any_field_of_the_event() -> None:
    assert digest(Event("a")) == digest(Event("a"))
    assert digest(Event("a")) != digest(Event("b"))
    assert len(digest(Event("a"))) == 16
