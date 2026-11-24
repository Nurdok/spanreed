"""A workout as Hevy returns it, and the notes that are written for it."""

from typing import Any

# Two exercises that are in the workout twice, a set with no weight, a set
# with neither weight nor reps, and names that file names cannot hold.
WORKOUT: dict[str, Any] = {
    "id": "w-1",
    "title": 'Push: day "A"',
    "description": "",
    "start_time": "2026-05-09T17:30:00+00:00",
    "end_time": "2026-05-09T18:40:00+00:00",
    "updated_at": "2026-05-09T18:41:00+00:00",
    "created_at": "2026-05-09T18:41:00+00:00",
    "exercises": [
        {
            "index": 0,
            "title": "Bench Press (Barbell)",
            "sets": [
                {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10},
                {
                    "index": 1,
                    "type": "normal",
                    "weight_kg": 82.5,
                    "reps": 8,
                    "rpe": 8.5,
                },
            ],
        },
        {
            "index": 1,
            "title": "Pull Up",
            "sets": [{"index": 0, "type": "normal", "weight_kg": None, "reps": 12}],
        },
        {
            "index": 2,
            "title": "Plank: side/front?",
            "sets": [
                {
                    "index": 0,
                    "type": "normal",
                    "weight_kg": None,
                    "reps": None,
                    "duration_seconds": 60,
                }
            ],
        },
        {
            "index": 3,
            "title": "Bench Press (Barbell)",
            "sets": [
                {
                    "index": 0,
                    "type": "failure",
                    "weight_kg": 60.0,
                    "reps": 15,
                    "rpe": 10,
                }
            ],
        },
    ],
}

# What the Hevy plugin wrote for WORKOUT while it wrote the notes itself,
# by path, without its empty weight_kg and reps lines: "Write note"
# leaves out a property that has no value.
EXPECTED_NOTES = {
    "Fitness/Sets/2026-05-09/Bench Press (Barbell) (1) set 1.md": (
        "---\n"
        "type: workout-set\n"
        "date: 2026-05-09\n"
        "exercise: Bench Press (Barbell)\n"
        "workout: 'Push: day \"A\"'\n"
        "workout_id: w-1\n"
        "exercise_index: 1\n"
        "set_number: 1\n"
        "set_ordinal: 1\n"
        "set_type: warmup\n"
        "weight_kg: 40\n"
        "reps: 10\n"
        "volume_kg: 400\n"
        "est_1rm_kg: 53.3\n"
        "tags:\n"
        "- workout-set\n"
        "---\n"
        "\n"
        "Bench Press (Barbell) — set 1: 40 kg × 10\n"
    ),
    "Fitness/Sets/2026-05-09/Bench Press (Barbell) (1) set 2.md": (
        "---\n"
        "type: workout-set\n"
        "date: 2026-05-09\n"
        "exercise: Bench Press (Barbell)\n"
        "workout: 'Push: day \"A\"'\n"
        "workout_id: w-1\n"
        "exercise_index: 1\n"
        "set_number: 2\n"
        "set_ordinal: 2\n"
        "set_type: normal\n"
        "weight_kg: 82.5\n"
        "reps: 8\n"
        "rpe: 8.5\n"
        "volume_kg: 660.0\n"
        "est_1rm_kg: 104.5\n"
        "tags:\n"
        "- workout-set\n"
        "---\n"
        "\n"
        "Bench Press (Barbell) — set 2: 82.5 kg × 8 @ RPE 8.5\n"
    ),
    "Fitness/Sets/2026-05-09/Pull Up set 1.md": (
        "---\n"
        "type: workout-set\n"
        "date: 2026-05-09\n"
        "exercise: Pull Up\n"
        "workout: 'Push: day \"A\"'\n"
        "workout_id: w-1\n"
        "exercise_index: 2\n"
        "set_number: 1\n"
        "set_ordinal: 3\n"
        "set_type: normal\n"
        "reps: 12\n"
        "tags:\n"
        "- workout-set\n"
        "---\n"
        "\n"
        "Pull Up — set 1: – kg × 12\n"
    ),
    "Fitness/Sets/2026-05-09/Plank sidefront set 1.md": (
        "---\n"
        "type: workout-set\n"
        "date: 2026-05-09\n"
        "exercise: 'Plank: side/front?'\n"
        "workout: 'Push: day \"A\"'\n"
        "workout_id: w-1\n"
        "exercise_index: 3\n"
        "set_number: 1\n"
        "set_ordinal: 4\n"
        "set_type: normal\n"
        "tags:\n"
        "- workout-set\n"
        "---\n"
        "\n"
        "Plank: side/front? — set 1: – kg × –\n"
    ),
    "Fitness/Sets/2026-05-09/Bench Press (Barbell) (2) set 1.md": (
        "---\n"
        "type: workout-set\n"
        "date: 2026-05-09\n"
        "exercise: Bench Press (Barbell)\n"
        "workout: 'Push: day \"A\"'\n"
        "workout_id: w-1\n"
        "exercise_index: 4\n"
        "set_number: 1\n"
        "set_ordinal: 5\n"
        "set_type: failure\n"
        "weight_kg: 60.0\n"
        "reps: 15\n"
        "rpe: 10\n"
        "volume_kg: 900.0\n"
        "est_1rm_kg: 90.0\n"
        "tags:\n"
        "- workout-set\n"
        "---\n"
        "\n"
        "Bench Press (Barbell) — set 1: 60 kg × 15 @ RPE 10\n"
    ),
}
