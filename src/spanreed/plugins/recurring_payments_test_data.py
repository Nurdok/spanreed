"""What the Recurring Payments plugin did before each payment became a workflow.

Made by running the plugin for one event, on 2026-11-24 at 18:00 UTC, of a
payment with the label ``therapy``, in the project ``P1``, of 150 each
time, with the task named by ``TEMPLATE`` and the note of ``LOG``.

A case says what differed: ``dates`` are the dates the task held (``None``:
there was no task), ``verify`` that the user is asked first, with the
``answers`` as places of the choices, ``cost`` and ``template`` another cost
and name, ``log`` ``None`` that there is no note, ``vault`` ``timeout`` that
Obsidian did not answer, ``around`` the text before and after the dates in
the comment, and ``tasks`` how many tasks had the label.

Each line of what the plugin did is one thing, in order:

- ``["tasks with label", label]``: Todoist was asked for the task.
- ``["yaml comment", task, create]``: the comment with the dates was read.
- ``["add task", fields]`` and ``["add comment", task, text]``
- ``["update task", task, fields]`` and ``["update comment", comment, text]``
- ``["due today", task]``
- ``["choice", question, choices, the place of the answer]``
- ``["append", path, text]``: the note was written to.
- ``["notify", message]``
- ``["fails", why]``: the plugin stopped with an error.
"""

from typing import Any

TEMPLATE = "Pay therapist ${{total_cost}} (for {{dates}})"
LOG = {
    "file_location": "Health/",
    "note_title": "Therapy",
    "note_content_template": "- {{date}}: session\n",
}

CASES: dict[str, dict[str, Any]] = {
    "the first date, a new task": {"dates": None},
    "one more date": {"dates": ["2026-11-10", "2026-11-17"]},
    "the date is on the task already": {"dates": ["2026-11-17", "2026-11-24"]},
    "asked first, yes": {"verify": True, "dates": ["2026-11-17"], "answers": [0]},
    "asked first, no": {"verify": True, "dates": ["2026-11-17"], "answers": [1]},
    "asked first, the date is there already": {"verify": True, "dates": ["2026-11-24"]},
    "a cost that is not whole": {"cost": 12.25, "dates": ["2026-11-17"]},
    "a cost that adds up to a whole": {"cost": 12.5, "dates": ["2026-11-17"]},
    "no cost, only dates": {
        "cost": 0.0,
        "template": "Allowance for {{dates}}",
        "dates": None,
    },
    "no note": {"log": None, "dates": ["2026-11-17"]},
    "the vault does not answer": {"dates": ["2026-11-17"], "vault": "timeout"},
    "text around the dates in the comment": {
        "dates": ["2026-11-17"],
        "around": ["Keep this.\n", "\nAnd this."],
    },
    "two tasks with the label": {"dates": ["2026-11-17"], "tasks": 2},
}

OLD_PLUGIN: dict[str, list[list[Any]]] = {
    "the first date, a new task": [
        ["tasks with label", "therapy"],
        [
            "add task",
            {
                "content": "Pay therapist $150 (for 2026-11-24)",
                "labels": ["therapy"],
                "project_id": "P1",
            },
        ],
        ["add comment", "T-new", "---\ndates:\n- '2026-11-24'\n---\n"],
        ["due today", "T-new"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "one more date": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $450 (for 2026-11-10, 2026-11-17, "
                "2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        [
            "update comment",
            "C1",
            "---\n"
            "dates:\n"
            "- '2026-11-10'\n"
            "- '2026-11-17'\n"
            "- '2026-11-24'\n"
            "---\n",
        ],
        ["due today", "T1"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "the date is on the task already": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
    ],
    "asked first, yes": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "choice",
            "You have a recurring payment of 150.0 due now (for task with "
            "label therapy).\n"
            "Would you like to add it to the list of dates?",
            ["Yes (add)", "No (skip)"],
            0,
        ],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $300 (for 2026-11-17, 2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        ["update comment", "C1", "---\ndates:\n- '2026-11-17'\n- '2026-11-24'\n---\n"],
        ["due today", "T1"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "asked first, no": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "choice",
            "You have a recurring payment of 150.0 due now (for task with "
            "label therapy).\n"
            "Would you like to add it to the list of dates?",
            ["Yes (add)", "No (skip)"],
            1,
        ],
    ],
    "asked first, the date is there already": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
    ],
    "a cost that is not whole": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $24.5 (for 2026-11-17, " "2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        ["update comment", "C1", "---\ndates:\n- '2026-11-17'\n- '2026-11-24'\n---\n"],
        ["due today", "T1"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "a cost that adds up to a whole": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $25 (for 2026-11-17, " "2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        [
            "update comment",
            "C1",
            "---\n" "dates:\n" "- '2026-11-17'\n" "- '2026-11-24'\n" "---\n",
        ],
        ["due today", "T1"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "no cost, only dates": [
        ["tasks with label", "therapy"],
        [
            "add task",
            {
                "content": "Allowance for 2026-11-24",
                "labels": ["therapy"],
                "project_id": "P1",
            },
        ],
        ["add comment", "T-new", "---\ndates:\n- '2026-11-24'\n---\n"],
        ["due today", "T-new"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "no note": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $300 (for 2026-11-17, 2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        ["update comment", "C1", "---\ndates:\n- '2026-11-17'\n- '2026-11-24'\n---\n"],
        ["due today", "T1"],
    ],
    "the vault does not answer": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $300 (for 2026-11-17, " "2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        ["update comment", "C1", "---\ndates:\n- '2026-11-17'\n- '2026-11-24'\n---\n"],
        ["due today", "T1"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
        [
            "notify",
            "Obsidian didn't answer, so 2026-11-24 was not added "
            "to “Therapy”. Add it by hand.",
        ],
    ],
    "text around the dates in the comment": [
        ["tasks with label", "therapy"],
        ["yaml comment", "T1", True],
        [
            "update task",
            "T1",
            {
                "content": "Pay therapist $300 (for " "2026-11-17, 2026-11-24)",
                "project_id": "P1",
            },
        ],
        ["yaml comment", "T1", False],
        [
            "update comment",
            "C1",
            "Keep this.\n"
            "---\n"
            "dates:\n"
            "- '2026-11-17'\n"
            "- '2026-11-24'\n"
            "---\n"
            "\n"
            "And this.",
        ],
        ["due today", "T1"],
        ["append", "Health/Therapy.md", "- 2026-11-24: session"],
    ],
    "two tasks with the label": [
        ["tasks with label", "therapy"],
        [
            "fails",
            "Expected either zero or exactly one task with the " "label therapy, got 2",
        ],
    ],
}
