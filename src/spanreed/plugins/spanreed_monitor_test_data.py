"""What the Spanreed Monitor plugin did before it became workflows.

Made by running the plugin on the cases below.

``EXCEPTIONS`` are cases of the errors that Spanreed kept: ``waiting`` is
what was in the list when Spanreed started, newest first, ``later`` what
came while it ran, and ``stopped`` that the plugin was stopped in the end.
Each line of what the plugin did is one thing, in order:

- ``["notify", text, how]``: a notification, and how its text was read.
- ``["send", text, how]``: a message.
- ``["dropped", texts]``: the errors that still waited were dropped.

``OBSIDIAN`` are cases of what the plugin for Obsidian said: each thing with
the minutes that passed before it, where ``None`` is a minute in which
nothing came. The plugin was started at minute 0. Each line of what it did
is ``["reported", minute, text]``: an error was put in the list of errors,
at that minute. It never did anything else: also after two hours without a
heartbeat it sent no message.
"""

from typing import Any

BEAT = {"kind": "watchdog"}
ERROR = {"kind": "error", "message": "Vault is locked"}

EXCEPTIONS: dict[str, dict[str, Any]] = {
    "nothing waits": {"waiting": []},
    "one exception waits": {"waiting": ["ValueError: boom\n"]},
    "two exceptions wait": {
        "waiting": ["KeyError: 'newest'\n", "ValueError: oldest\n"]
    },
    "an exception comes later": {"waiting": [], "later": ["RuntimeError: later\n"]},
    "stopped": {"waiting": [], "stopped": True},
}

OBSIDIAN: dict[str, list[tuple[float, Any]]] = {
    "heartbeats, then two hours of silence": [
        (0.5, BEAT),
        (0.5, BEAT),
        *[(1, None)] * 120,
    ],
    "silence from the start": [*[(1, None)] * 45],
    "an error in the first half hour": [(10, ERROR)],
    "an error after the first half hour": [*[(0.5, BEAT)] * 61, (0.5, ERROR)],
    "two errors, ten minutes apart": [
        *[(0.5, BEAT)] * 61,
        (0.5, ERROR),
        *[(0.5, BEAT)] * 20,
        (0, ERROR),
    ],
    "two errors, forty minutes apart": [
        *[(0.5, BEAT)] * 61,
        (0.5, ERROR),
        *[(0.5, BEAT)] * 80,
        (0, ERROR),
    ],
    "an error that is ignored": [
        *[(0.5, BEAT)] * 61,
        (0.5, {"kind": "error", "message": "read ECONNRESET"}),
    ],
    "an error with more in it": [
        *[(0.5, BEAT)] * 61,
        (
            0.5,
            {
                "kind": "error",
                "message": "Cannot read file",
                "stack": "at read (main.js:1)",
            },
        ),
    ],
}

OLD_PLUGIN: dict[str, dict[str, list[list[Any]]]] = {
    "exceptions": {
        "nothing waits": [["notify", "Spanreed is starting up.", {}]],
        "one exception waits": [
            ["notify", "Spanreed is starting up.", {}],
            [
                "notify",
                "Exception retrieved from storage:\n"
                "\n"
                "```python\n"
                "ValueError: boom\n"
                "\n"
                "```",
                {"parse_html": False, "parse_markdown": True},
            ],
            ["notify", "Spanreed is still running.", {}],
            ["dropped", []],
        ],
        "two exceptions wait": [
            ["notify", "Spanreed is starting up.", {}],
            [
                "notify",
                "Exception retrieved from storage:\n"
                "\n"
                "```python\n"
                "KeyError: 'newest'\n"
                "\n"
                "```",
                {"parse_html": False, "parse_markdown": True},
            ],
            ["notify", "Spanreed is still running.", {}],
            ["dropped", ["ValueError: oldest\n"]],
        ],
        "an exception comes later": [
            ["notify", "Spanreed is starting up.", {}],
            [
                "notify",
                "Exception retrieved from storage:\n"
                "\n"
                "```python\n"
                "RuntimeError: later\n"
                "\n"
                "```",
                {"parse_html": False, "parse_markdown": True},
            ],
            ["notify", "Spanreed is still running.", {}],
            ["dropped", []],
        ],
        "stopped": [
            ["notify", "Spanreed is starting up.", {}],
            ["send", "Spanreed is shutting down.", {}],
        ],
    },
    "obsidian": {
        "heartbeats, then two hours of silence": [],
        "silence from the start": [],
        "an error in the first half hour": [],
        "an error after the first half hour": [
            [
                "reported",
                31.0,
                "Obsidian plugin error: {'kind': "
                "'error', 'message': 'Vault is "
                "locked'}",
            ]
        ],
        "two errors, ten minutes apart": [
            [
                "reported",
                31.0,
                "Obsidian plugin error: {'kind': "
                "'error', 'message': 'Vault is "
                "locked'}",
            ]
        ],
        "two errors, forty minutes apart": [
            [
                "reported",
                31.0,
                "Obsidian plugin error: {'kind': "
                "'error', 'message': 'Vault is "
                "locked'}",
            ],
            [
                "reported",
                71.0,
                "Obsidian plugin error: {'kind': "
                "'error', 'message': 'Vault is "
                "locked'}",
            ],
        ],
        "an error that is ignored": [],
        "an error with more in it": [
            [
                "reported",
                31.0,
                "Obsidian plugin error: {'kind': 'error', "
                "'message': 'Cannot read file', 'stack': "
                "'at read (main.js:1)'}",
            ]
        ],
    },
}
