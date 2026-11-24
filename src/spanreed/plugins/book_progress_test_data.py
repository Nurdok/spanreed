"""What the Audible Book Progress plugin did before it became a workflow.

Made by running the plugin's sync for one user, once for each case below.

A case says what there was: ``notes`` are the book notes that the vault's
query found, as ``[path, title, asin]``; ``library`` is the Audible library,
as ``[asin, title, percent listened, finished]``; ``properties`` holds what
the notes had, by ``path|property``; ``answers`` are the places of the
user's answers. A case with ``syncs`` ran that many times in a row, and one
with ``by_hand`` ran from the plugin's command.

Each line of what the plugin did is one thing, in order:

- ``["query", text]``: the vault was asked for the notes.
- ``["library"]``: Audible was asked for the library.
- ``["get", note, property]`` and ``["set", note, property, value]``
- ``["choice", question, choices, the place of the answer]``
- ``["remember", name]``: the plugin kept something for later.
- ``["notify", message]``
- ``["again"]``: the next sync of the case starts here.

``<today>`` stands for the date of the day it ran.
"""

from typing import Any

CASES: dict[str, dict[str, Any]] = {
    "linked, progress changed": {
        "notes": [["Books/Kings.md", "The Way of Kings", "B1"]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight Archive, " "Book 1", 42.4, False],
            ["B2", "Project Hail Mary", 0.0, False],
        ],
        "properties": {"Books/Kings.md|progress": 30},
    },
    "linked, progress the same": {
        "notes": [["Books/Kings.md", "The Way of Kings", "B1"]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight Archive, " "Book 1", 42.4, False]
        ],
        "properties": {"Books/Kings.md|progress": 42},
    },
    "unlinked, book chosen": {
        "notes": [["Books/Kings.md", "The Way of Kings", None]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight Archive, " "Book 1", 10.0, False],
            ["B2", "Project Hail Mary", 0.0, False],
        ],
        "answers": [0],
    },
    "unlinked, not on Audible, then again": {
        "notes": [["Books/Kings.md", "The Way of Kings", None]],
        "library": [
            ["B1", "The Way of Kings: The " "Stormlight Archive, Book 1", 10.0, False]
        ],
        "answers": [1],
        "syncs": 2,
    },
    "unlinked, ask me later, then again": {
        "notes": [["Books/Kings.md", "The Way of Kings", None]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight " "Archive, Book 1", 10.0, False]
        ],
        "answers": [2],
        "syncs": 2,
    },
    "unlinked, nothing close, then again": {
        "notes": [["Books/Kings.md", "The Way of Kings", None]],
        "library": [["B2", "Project Hail Mary", 5.0, False]],
        "syncs": 2,
    },
    "finished, marked as read, then again": {
        "notes": [["Books/Kings.md", "The Way of Kings", "B1"]],
        "library": [
            ["B1", "The Way of Kings: The " "Stormlight Archive, Book 1", 100.0, True]
        ],
        "properties": {"Books/Kings.md|progress": 90},
        "answers": [0],
        "syncs": 2,
    },
    "nearly finished, not marked, then again": {
        "notes": [["Books/Kings.md", "The Way of Kings", "B1"]],
        "library": [
            ["B1", "The Way of Kings: The " "Stormlight Archive, Book 1", 99.2, False]
        ],
        "properties": {"Books/Kings.md|progress": 99},
        "answers": [1],
        "syncs": 2,
    },
    "an ASIN that is not in the library": {
        "notes": [["Books/Kings.md", "The Way of Kings", "BX"]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight " "Archive, Book 1", 10.0, False]
        ],
    },
    "no notes": {
        "notes": [],
        "library": [
            ["B1", "The Way of Kings: The Stormlight Archive, Book 1", 10.0, False]
        ],
    },
    "a note without a title": {
        "notes": [["Books/Dune.md", None, None]],
        "library": [
            ["B3", "Dune", 7.0, False],
            ["B2", "Project Hail Mary", 0.0, False],
        ],
        "answers": [0],
    },
    "two notes, the first for later": {
        "notes": [
            ["Books/Kings.md", "The Way of Kings", None],
            ["Books/Mary.md", "Project Hail Mary", "B2"],
        ],
        "library": [
            ["B1", "The Way of Kings: The Stormlight " "Archive, Book 1", 10.0, False],
            ["B2", "Project Hail Mary", 55.5, False],
        ],
        "answers": [2],
    },
    "by hand, nothing changed": {
        "notes": [["Books/Kings.md", "The Way of Kings", "B1"]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight Archive, " "Book 1", 42.4, False]
        ],
        "properties": {"Books/Kings.md|progress": 42},
        "by_hand": True,
    },
    "by hand, progress changed": {
        "notes": [["Books/Kings.md", "The Way of Kings", "B1"]],
        "library": [
            ["B1", "The Way of Kings: The Stormlight Archive, " "Book 1", 42.4, False]
        ],
        "properties": {"Books/Kings.md|progress": 30},
        "by_hand": True,
    },
}

OLD_PLUGIN: dict[str, list[list[Any]]] = {
    "linked, progress changed": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
        ["set", "Books/Kings.md", "progress", 42],
    ],
    "linked, progress the same": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
    ],
    "unlinked, book chosen": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        [
            "choice",
            'Which Audible book is "The Way of Kings"?',
            [
                "The Way of Kings: The Stormlight Archive, Book 1",
                "Not on Audible",
                "Ask me later",
            ],
            0,
        ],
        ["set", "Books/Kings.md", "asin", "B1"],
        ["get", "Books/Kings.md", "progress"],
        ["set", "Books/Kings.md", "progress", 10],
    ],
    "unlinked, not on Audible, then again": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        [
            "choice",
            'Which Audible book is "The Way of Kings"?',
            [
                "The Way of Kings: The Stormlight Archive, " "Book 1",
                "Not on Audible",
                "Ask me later",
            ],
            1,
        ],
        ["remember", "not-audible:Books/Kings.md"],
        ["again"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
    ],
    "unlinked, ask me later, then again": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        [
            "choice",
            'Which Audible book is "The Way of Kings"?',
            [
                "The Way of Kings: The Stormlight Archive, " "Book 1",
                "Not on Audible",
                "Ask me later",
            ],
            2,
        ],
        ["remember", "link-snooze:Books/Kings.md"],
        ["again"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
    ],
    "unlinked, nothing close, then again": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["remember", "link-snooze:Books/Kings.md"],
        ["again"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
    ],
    "finished, marked as read, then again": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
        ["set", "Books/Kings.md", "progress", 100],
        [
            "choice",
            'Looks like you finished "The Way of Kings: '
            'The Stormlight Archive, Book 1" on '
            "Audible.\n"
            "Mark it as read?",
            ["Yes", "No"],
            0,
        ],
        ["remember", "finish-prompted:B1"],
        ["set", "Books/Kings.md", "status", "read"],
        ["set", "Books/Kings.md", "finish-date", "<today>"],
        ["again"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
    ],
    "nearly finished, not marked, then again": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
        [
            "choice",
            'Looks like you finished "The Way of '
            'Kings: The Stormlight Archive, Book 1" '
            "on Audible.\n"
            "Mark it as read?",
            ["Yes", "No"],
            1,
        ],
        ["remember", "finish-prompted:B1"],
        ["again"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
    ],
    "an ASIN that is not in the library": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
    ],
    "no notes": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ]
    ],
    "a note without a title": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        [
            "choice",
            'Which Audible book is "Dune"?',
            ["Dune", "Not on Audible", "Ask me later"],
            0,
        ],
        ["set", "Books/Dune.md", "asin", "B3"],
        ["get", "Books/Dune.md", "progress"],
        ["set", "Books/Dune.md", "progress", 7],
    ],
    "two notes, the first for later": [
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        [
            "choice",
            'Which Audible book is "The Way of Kings"?',
            [
                "The Way of Kings: The Stormlight Archive, Book " "1",
                "Not on Audible",
                "Ask me later",
            ],
            2,
        ],
        ["remember", "link-snooze:Books/Kings.md"],
        ["get", "Books/Mary.md", "progress"],
        ["set", "Books/Mary.md", "progress", 56],
    ],
    "by hand, nothing changed": [
        ["notify", "Syncing Audible book progress…"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
        ["notify", "No book progress changes found."],
    ],
    "by hand, progress changed": [
        ["notify", "Syncing Audible book progress…"],
        [
            "query",
            "\n"
            "        table title, asin\n"
            "        from #book\n"
            '        where status = "reading"\n'
            "        ",
        ],
        ["library"],
        ["get", "Books/Kings.md", "progress"],
        ["set", "Books/Kings.md", "progress", 42],
    ],
}
