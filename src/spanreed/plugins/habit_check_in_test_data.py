"""What the Habit Tracker plugin did before it became a workflow.

Made by running the plugin's check for one user, once for each day below.
The user's habits were ``HABITS``, kept in the property ``habits`` of the
daily notes in ``Daily``. Each line is what happened, in order:

- ``["create"]``: today's daily note was asked for.
- ``["get", note, property]``: the property was read.
- ``["choice", question, choices, columns, the place of the answer]``
- ``["add", note, property, value]``: a habit was marked as done.
- ``["say", message]``
"""

from typing import Any

NOTE = "Daily/2026-11-24.md"
HABITS = ["meditate", "exercise", "read", "take vitamin D"]
# What stands for a note that is not there when it is first read.
MISSING = "the note is missing"

# By the name of the day: what the property held.
HELD: dict[str, Any] = {
    "one done, one picked, then cancel": ["exercise"],
    "all done": ["meditate", "exercise", "read", "take vitamin D"],
    "none done, all picked": None,
    "not a list": "exercise",
    "the note is missing at first": MISSING,
}

OLD_PLUGIN: dict[str, list[list[Any]]] = {
    "one done, one picked, then cancel": [
        ["create"],
        ["get", "Daily/2026-11-24.md", "habits"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Meditate", "Read", "Take vitamin d", ["Cancel"]],
            4,
            0,
        ],
        ["add", "Daily/2026-11-24.md", "habits", "meditate"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Read", "Take vitamin d", ["Cancel"]],
            4,
            2,
        ],
    ],
    "all done": [["create"], ["get", "Daily/2026-11-24.md", "habits"]],
    "none done, all picked": [
        ["create"],
        ["get", "Daily/2026-11-24.md", "habits"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Meditate", "Exercise", "Read", "Take vitamin d", ["Cancel"]],
            4,
            0,
        ],
        ["add", "Daily/2026-11-24.md", "habits", "meditate"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Exercise", "Read", "Take vitamin d", ["Cancel"]],
            4,
            0,
        ],
        ["add", "Daily/2026-11-24.md", "habits", "exercise"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Read", "Take vitamin d", ["Cancel"]],
            4,
            0,
        ],
        ["add", "Daily/2026-11-24.md", "habits", "read"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Take vitamin d", ["Cancel"]],
            4,
            0,
        ],
        ["add", "Daily/2026-11-24.md", "habits", "take vitamin D"],
    ],
    "not a list": [
        ["create"],
        ["get", "Daily/2026-11-24.md", "habits"],
        ["say", "Invalid value for habits: 'exercise'; expected a list of strings."],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Meditate", "Exercise", "Read", "Take vitamin d", ["Cancel"]],
            4,
            4,
        ],
    ],
    "the note is missing at first": [
        ["create"],
        ["get", "Daily/2026-11-24.md", "habits"],
        ["say", "Generating today's daily note..."],
        ["create"],
        ["get", "Daily/2026-11-24.md", "habits"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Meditate", "Exercise", "Read", "Take vitamin d", ["Cancel"]],
            4,
            1,
        ],
        ["add", "Daily/2026-11-24.md", "habits", "exercise"],
        [
            "choice",
            "Did you do any of these habits today?",
            ["Meditate", "Read", "Take vitamin d", ["Cancel"]],
            4,
            3,
        ],
    ],
}
