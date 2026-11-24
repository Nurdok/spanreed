import asyncio
import datetime
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.hevy import Workout
from spanreed.apis.hevy_blocks import (
    EPOCH,
    NewWorkout,
    WorkoutChoice,
    WorkoutEvent,
    WorkoutSet,
    hevy_blocks,
    workout_event,
)
from spanreed.apis.hevy_test_data import WORKOUT
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.blocks import (
    BlockRegistry,
    ManualError,
    Repeat,
    TriggerContext,
)
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.testing import fake_plugin

NOW = datetime.datetime(2026, 5, 9, 19, 0, 30, tzinfo=datetime.timezone.utc)


class _State:
    """A trigger's state, kept in a dict."""

    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


def _context(**state: str) -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=_State(**state),
        queue=WorkflowQueue("wf_1"),
    )


def _workout(workout_id: str = "w-1", start: str = "2026-05-09T17:30:00Z") -> Workout:
    return Workout.from_json({**WORKOUT, "id": workout_id, "start_time": start})


@pytest.fixture(autouse=True)
def hevy() -> Iterator[AsyncMock]:
    """Stand in for the Hevy API. A test sets what it returns."""
    api = AsyncMock()
    api.get_updated_workouts_since.return_value = []
    api.get_all_workouts.return_value = []
    with (
        patch(
            "spanreed.apis.hevy_blocks.HevyApi.for_user",
            new=AsyncMock(return_value=api),
        ),
        patch("spanreed.apis.hevy_blocks._now", new=lambda: NOW),
    ):
        yield api


def test_the_hevy_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in hevy_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == ["hevy.new-workout"]


# -- a workout as an event -----------------------------------------------------


def test_a_workout_gives_its_sets_in_the_order_they_were_done() -> None:
    event = workout_event(_workout())

    assert (event.id, event.title) == ("w-1", 'Push: day "A"')
    assert (event.date, event.folder) == ("2026-05-09", "2026-05-09")
    assert (event.set_count, event.exercise_count, event.new) == (5, 4, True)
    assert [
        (s.exercise_index, s.set_number, s.set_ordinal, s.note_name) for s in event.sets
    ] == [
        # An exercise that is in the workout twice gets a number.
        (1, 1, 1, "Bench Press (Barbell) (1) set 1"),
        (1, 2, 2, "Bench Press (Barbell) (1) set 2"),
        (2, 1, 3, "Pull Up set 1"),
        # What a file name cannot hold is left out of the name.
        (3, 1, 4, "Plank sidefront set 1"),
        (4, 1, 5, "Bench Press (Barbell) (2) set 1"),
    ]


def test_a_set_comes_with_its_volume_its_estimated_maximum_and_a_line() -> None:
    event = workout_event(_workout())

    assert event.sets[1] == WorkoutSet(
        exercise="Bench Press (Barbell)",
        exercise_index=1,
        set_number=2,
        set_ordinal=2,
        set_type="normal",
        weight_kg=82.5,
        reps=8,
        rpe=8.5,
        distance_meters=None,
        duration_seconds=None,
        volume_kg=660.0,
        # Epley: 82.5 * (1 + 8 / 30).
        est_1rm_kg=104.5,
        note_name="Bench Press (Barbell) (1) set 2",
        summary="Bench Press (Barbell) — set 2: 82.5 kg × 8 @ RPE 8.5",
    )


def test_a_set_without_weight_or_reps_has_no_volume_and_says_so() -> None:
    pull_up, plank = workout_event(_workout()).sets[2:4]

    assert (pull_up.volume_kg, pull_up.est_1rm_kg) == (None, None)
    assert pull_up.summary == "Pull Up — set 1: – kg × 12"
    assert plank.summary == "Plank: side/front? — set 1: – kg × –"
    assert plank.duration_seconds == 60


def test_a_workout_with_no_start_is_dated_by_when_it_was_made() -> None:
    workout = _workout(start="")
    assert workout_event(workout).date == "2026-05-09"

    workout.created_at = "not a time"
    event = workout_event(workout)
    # Its notes go into a folder named after the workout.
    assert (event.date, event.folder) == (None, "w-1")


# -- polls ---------------------------------------------------------------------


def test_the_first_poll_asks_for_every_workout(hevy: AsyncMock) -> None:
    hevy.get_updated_workouts_since.return_value = [_workout("a"), _workout("b")]

    poll = asyncio.run(NewWorkout().poll(_context(), Repeat(), None))

    hevy.get_updated_workouts_since.assert_awaited_once_with(EPOCH)
    assert [event.id for event in poll.events] == ["a", "b"]
    # The next poll starts at the time this one was made.
    assert poll.cursor == "2026-05-09T19:00:30Z"


def test_a_later_poll_asks_for_what_changed_since_the_cursor(hevy: AsyncMock) -> None:
    asyncio.run(NewWorkout().poll(_context(), Repeat(), "2026-05-09T18:00:00Z"))

    hevy.get_updated_workouts_since.assert_awaited_once_with("2026-05-09T18:00:00Z")


def test_a_workout_fires_once_whatever_changes_in_it() -> None:
    trigger = NewWorkout()
    first = workout_event(_workout())
    changed = workout_event(_workout())
    changed.title = "Renamed"

    assert trigger.key(first) == "w-1"
    assert trigger.version(first) == trigger.version(changed)


# -- by hand -------------------------------------------------------------------


def _by_hand(ctx: TriggerContext | None = None, **answer: Any) -> list[WorkoutEvent]:
    return asyncio.run(
        NewWorkout().manual_events(ctx or _context(), Repeat(), WorkoutChoice(**answer))
    )


def test_by_hand_new_workouts_are_the_ones_the_workflow_did_not_run_for(
    hevy: AsyncMock,
) -> None:
    hevy.get_updated_workouts_since.return_value = [_workout("a"), _workout("b")]
    ctx = _context(cursor="2026-05-09T18:00:00Z", seen=json.dumps({"a": ""}))

    events = _by_hand(ctx, which="New workouts")

    assert [(event.id, event.new) for event in events] == [("b", True)]
    hevy.get_updated_workouts_since.assert_awaited_once_with("2026-05-09T18:00:00Z")


def test_by_hand_a_date_gives_the_workouts_of_that_day(hevy: AsyncMock) -> None:
    hevy.get_all_workouts.return_value = [
        _workout("a", "2026-05-08T17:00:00Z"),
        _workout("b", "2026-05-09T07:00:00Z"),
        _workout("c", "2026-05-09T17:00:00Z"),
    ]
    ctx = _context(seen=json.dumps({"b": ""}))

    events = _by_hand(ctx, which="A specific date", date="2026-05-09")

    # A workout that was written before is not new: it is written again
    # without being announced again.
    assert [(event.id, event.new) for event in events] == [("b", False), ("c", True)]


def test_by_hand_a_date_that_is_no_date_or_has_no_workout_says_so(
    hevy: AsyncMock,
) -> None:
    with pytest.raises(ManualError, match="doesn’t look like a YYYY-MM-DD date"):
        _by_hand(which="A specific date", date="May 9th")
    with pytest.raises(ManualError, match="No workout found on 2026-05-09."):
        _by_hand(which="A specific date", date="2026-05-09")


def test_by_hand_a_workout_id_gives_that_workout(hevy: AsyncMock) -> None:
    hevy.get_workout.return_value = _workout("abc")

    (event,) = _by_hand(which="A workout ID", workout_id="abc")

    hevy.get_workout.assert_awaited_once_with("abc")
    assert event.id == "abc"


def test_by_hand_a_workout_id_that_hevy_does_not_have_says_so(
    hevy: AsyncMock,
) -> None:
    hevy.get_workout.side_effect = RuntimeError("404")

    with pytest.raises(ManualError, match="Couldn’t fetch the workout nope."):
        _by_hand(which="A workout ID", workout_id="nope")


def test_by_hand_all_workouts_gives_every_workout(hevy: AsyncMock) -> None:
    hevy.get_all_workouts.return_value = [_workout("a"), _workout("b")]

    assert [event.id for event in _by_hand(which="All workouts")] == ["a", "b"]
