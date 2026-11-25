"""The Hevy trigger for workflows.

The plugin holds the API key. The trigger here asks Hevy, at an interval,
for the workouts that are new since the last time, and gives each as an
event. A workout comes with its sets as a flat list, in the order they
were done, so that a workflow can write a note for each set.
"""

from __future__ import annotations

import collections
import datetime
import re
from dataclasses import dataclass, field
from typing import Literal

from spanreed.apis.hevy import HevyApi, Workout
from spanreed.config_form import described
from spanreed.workflows.blocks import (
    Block,
    ManualError,
    Poll,
    PollTrigger,
    Repeat,
    TriggerContext,
)

# The cursor of the first poll of a user: Hevy returns every workout.
EPOCH = "1970-01-01T00:00:00Z"


@dataclass
class WorkoutSet:
    """One set of a workout."""

    exercise: str = described("The name of the exercise")
    exercise_index: int = described("The place of the exercise in the workout, from 1")
    set_number: int = described("The place of the set in its exercise, from 1")
    set_ordinal: int = described(
        "The place of the set in the whole workout, from 1. Sorting by it "
        "gives the order the sets were done in",
    )
    set_type: str = described(
        "The kind of set: normal, warmup, failure or dropset",
    )
    weight_kg: float | None = described(
        "The weight, in kilograms. Nothing for a set without one",
    )
    reps: int | None = described(
        "How many repetitions. Nothing for a set without them",
    )
    rpe: float | None = described(
        "The rate of perceived exertion. Nothing if you did not log it",
    )
    distance_meters: float | None = described(
        "The distance, in meters. Nothing for a set without one",
    )
    duration_seconds: int | None = described(
        "How long the set took, in seconds. Nothing for a set without a " "time",
    )
    volume_kg: float | None = described("Weight times repetitions, in kilograms")
    est_1rm_kg: float | None = described(
        "The estimated one-rep maximum, by the Epley formula, in kilograms",
    )
    note_name: str = described(
        "A name for a note of the set that is safe as a file name and "
        "unlike the names of the workout’s other sets: Squat set 1",
    )
    summary: str = described(
        "The set in a line: Squat — set 1: 100 kg × 5 @ RPE 8",
    )


@dataclass
class WorkoutEvent:
    """A workout, with its sets in the order they were done."""

    id: str = described("The id that Hevy gives the workout")
    title: str = described("The title of the workout")
    date: str | None = described(
        "The day the workout started, as 2026-05-09, as Hevy dates it. "
        "Nothing if Hevy gave no time for it",
    )
    folder: str = described(
        "A name for a folder of the workout’s notes: the date, or the id of "
        "a workout that has none",
    )
    sets: list[WorkoutSet] = described(
        "The sets of the workout, in the order they were done",
    )
    set_count: int = described("How many sets the workout has")
    exercise_count: int = described("How many exercises the workout has")
    new: bool = described(
        "Whether the workflow has not run for this workout before",
        default=True,
    )


def _sanitize(name: str) -> str:
    """Return a name without the characters that file names cannot hold."""
    return re.sub(r'[\\/:*?"<>|]', "", name).strip() or "Exercise"


def _parse_time(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def workout_date(workout: Workout) -> datetime.date | None:
    """Return the day a workout started, or was created if it has no start."""
    time = _parse_time(workout.start_time) or _parse_time(workout.created_at)
    return time.date() if time else None


def _est_1rm(weight_kg: float | None, reps: int | None) -> float | None:
    # The Epley formula.
    if not weight_kg or not reps:
        return None
    return round(weight_kg * (1 + reps / 30), 1)


def workout_event(workout: Workout, new: bool = True) -> WorkoutEvent:
    """Return a workout as the event of the trigger."""
    date = workout_date(workout)
    # An exercise can be in a workout more than once. Its sets are told
    # apart by a number after the name, only then.
    times = collections.Counter(exercise.title for exercise in workout.exercises)
    seen: dict[str, int] = collections.defaultdict(int)

    sets: list[WorkoutSet] = []
    for exercise_index, exercise in enumerate(workout.exercises, start=1):
        seen[exercise.title] += 1
        suffix = f" ({seen[exercise.title]})" if times[exercise.title] > 1 else ""
        for set_number, done in enumerate(exercise.sets, start=1):
            volume_kg = None
            if done.weight_kg is not None and done.reps is not None:
                volume_kg = round(done.weight_kg * done.reps, 1)
            weight = f"{done.weight_kg:g}" if done.weight_kg is not None else "–"
            reps = done.reps if done.reps is not None else "–"
            summary = f"{exercise.title} — set {set_number}: {weight} kg × {reps}"
            if done.rpe is not None:
                summary += f" @ RPE {done.rpe:g}"
            sets.append(
                WorkoutSet(
                    exercise=exercise.title,
                    exercise_index=exercise_index,
                    set_number=set_number,
                    set_ordinal=len(sets) + 1,
                    set_type=done.type,
                    weight_kg=done.weight_kg,
                    reps=done.reps,
                    rpe=done.rpe,
                    distance_meters=done.distance_meters,
                    duration_seconds=done.duration_seconds,
                    volume_kg=volume_kg,
                    est_1rm_kg=_est_1rm(done.weight_kg, done.reps),
                    note_name=f"{_sanitize(exercise.title)}{suffix} set {set_number}",
                    summary=summary,
                )
            )
    return WorkoutEvent(
        id=workout.id,
        title=workout.title,
        date=date.isoformat() if date else None,
        folder=date.isoformat() if date else workout.id,
        sets=sets,
        set_count=len(sets),
        exercise_count=len(workout.exercises),
        new=new,
    )


def _now() -> datetime.datetime:
    # Indirection so that tests can set the time of a poll.
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class WorkoutChoice:
    """Which workouts a manual run covers."""

    which: Literal[
        "New workouts", "A specific date", "A workout ID", "All workouts"
    ] = field(metadata={"prompt": "Write Hevy workouts for…"})
    date: str = field(
        default="",
        metadata={
            "prompt": "Enter the date (YYYY-MM-DD):",
            "ask_if": {"which": "A specific date"},
        },
    )
    workout_id: str = field(
        default="",
        metadata={
            "prompt": "Enter the Hevy workout ID:",
            "ask_if": {"which": "A workout ID"},
        },
    )
    confirm: Literal["Yes"] = field(
        default="Yes",
        metadata={
            "prompt": "Write the notes of ALL workouts again?",
            "ask_if": {"which": "All workouts"},
        },
    )


class NewWorkout(PollTrigger[Repeat, WorkoutEvent]):
    """Fire for each workout that Hevy has and the workflow did not run for.

    A workout fires once: a workout that is changed in Hevy afterwards
    does not fire again. A manual run can write it again.
    """

    id = "hevy.new-workout"
    title = "New workout"
    Manual = WorkoutChoice
    # Every workout: one that is forgotten fires again when it is changed.
    remembers = 20000

    async def poll(
        self, ctx: TriggerContext, params: Repeat, cursor: str | None
    ) -> Poll[WorkoutEvent]:
        """Return the workouts that were made or changed since the cursor."""
        # Taken before asking, so that a workout logged while Hevy answers
        # is found by the next poll. One that is found twice runs once.
        started = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        hevy = await HevyApi.for_user(ctx.user)
        workouts = await hevy.get_updated_workouts_since(cursor or EPOCH)
        return Poll(events=[workout_event(w) for w in workouts], cursor=started)

    def key(self, event: WorkoutEvent) -> str:
        """Return the id of the workout."""
        return event.id

    async def manual_events(
        self, ctx: TriggerContext, params: Repeat, answer: WorkoutChoice
    ) -> list[WorkoutEvent]:
        """Return the new workouts, or the ones to write again.

        A workout that the workflow ran for before is not ``new``.
        """
        if answer.which == "New workouts":
            return await self.new_events(ctx, params)

        hevy = await HevyApi.for_user(ctx.user)
        workouts: list[Workout]
        if answer.which == "A specific date":
            try:
                date = datetime.date.fromisoformat(answer.date)
            except ValueError:
                raise ManualError("That doesn’t look like a YYYY-MM-DD date.") from None
            workouts = [
                workout
                for workout in await hevy.get_all_workouts()
                if workout_date(workout) == date
            ]
            if not workouts:
                raise ManualError(f"No workout found on {date.isoformat()}.")
        elif answer.which == "A workout ID":
            try:
                workouts = [await hevy.get_workout(answer.workout_id)]
            except Exception:
                raise ManualError(
                    f"Couldn’t fetch the workout {answer.workout_id}."
                ) from None
        else:
            workouts = await hevy.get_all_workouts()

        seen = await self._seen(ctx)
        return [workout_event(w, new=w.id not in seen) for w in workouts]


def hevy_blocks() -> list[Block]:
    """Return the blocks that the Hevy plugin publishes."""
    return [NewWorkout()]
