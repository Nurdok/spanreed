"""What the Lit Notes plugin asked, said and wrote before it became a workflow.

Made by running the plugin's "Add book note" on the first books of
``sample_books`` with the templates below, once for each conversation. Each
line is what happened, in order:

- ``["input", question, answer]``
- ``["choice", question, choices, the place of the answer]``
- ``["say", message]``
- ``["append", path, text]``: what was written to the vault.
"""

import datetime
from typing import Any

from spanreed.apis.google_books import Book


def sample_books(count: int) -> list[Book]:
    """Return the first ``count`` books that the search finds in the tests."""
    books = [
        Book(
            "Dune",
            ["Frank Herbert"],
            "Ace",
            datetime.date(1965, 8, 1),
            "Sand.",
            "https://covers/dune",
        ),
        Book(
            "Good Omens: The Nice and Accurate Prophecies",
            ["Terry Pratchett", "Neil Gaiman"],
            "Gollancz",
            datetime.date(1990, 5, 10),
            "An angel and a demon.",
            "https://covers/omens",
        ),
        Book(
            "Design Patterns",
            ["Erich Gamma", "Richard Helm", "Ralph Johnson", "John Vlissides"],
            "Addison-Wesley",
            datetime.date(1994, 10, 21),
            "Twenty-three patterns.",
            "",
        ),
        Book("Emma", ["Jane Austen"], "John Murray", None, "", "https://covers/emma"),
        Book(
            "Children of Dune",
            ["Frank Herbert"],
            "Putnam",
            datetime.date(1976, 4, 1),
            "More sand.",
            "https://covers/children",
        ),
        Book(
            "Dune Messiah",
            ["Frank Herbert"],
            "Putnam",
            datetime.date(1969, 1, 1),
            "Even more sand.",
            "https://covers/messiah",
        ),
        Book(
            "What If? Serious Answers",
            ["Randall Munroe"],
            "Houghton",
            datetime.date(2014, 9, 2),
            "Absurd questions.",
            "https://covers/whatif",
        ),
    ]
    return books[:count]


TITLE_TEMPLATE = "{{ book.short_title }}"
CONTENT_TEMPLATE = """---
tags: book
cover: {{ book.cover_image_url }}
---
# {{ book.full_title }}
author:: {{ book.formatted_authors }}
year:: {{ book.publication_year }}
publisher:: {{ book.publisher }}
{% for person in recommended_by %}recommended_by:: [[{{ person }}]]
{% endfor %}
{{ book.description }}

{{ free_text }}
"""

# By the name of the conversation: how many books the search found, and
# what the plugin did.
BOOKS_FOUND = {
    "many, show more, with recommendations and free text": 7,
    "one, confirmed, nothing added": 1,
    "one, not the one": 1,
    "many, cancelled": 7,
    "none found": 0,
    "many, the second, two authors": 7,
}

OLD_PLUGIN: dict[str, list[list[Any]]] = {
    "many, show more, with recommendations and free text": [
        ["input", "Which book do you want to " "add a note for?", "dune"],
        [
            "choice",
            "Found multiple books. Which " "one did you mean?",
            [
                "Dune by Frank Herbert " "(1965)",
                "Good Omens: The Nice and "
                "Accurate Prophecies by "
                "Terry Pratchettand Neil "
                "Gaiman (1990)",
                "Design Patterns by Erich "
                "Gamma, Richard Helm, Ralph "
                "Johnsonand John Vlissides "
                "(1994)",
                "Emma by Jane Austen " "(Unknown)",
                "Children of Dune by Frank " "Herbert (1976)",
                "Show more",
                "Cancel",
            ],
            5,
        ],
        [
            "choice",
            "Found multiple books. Which " "one did you mean?",
            [
                "Dune Messiah by Frank " "Herbert (1969)",
                "What If? Serious Answers " "by Randall Munroe (2014)",
                "Show more",
                "Cancel",
            ],
            1,
        ],
        ["choice", 'Add "Recommended by"?', ["Yes", "No"], 0],
        ["input", "Who recommended it?", "Dana"],
        ["choice", 'Add another "Recommended ' 'by"?', ["Yes", "No"], 0],
        ["input", "Who recommended it?", "Omer"],
        ["choice", 'Add another "Recommended ' 'by"?', ["Yes", "No"], 1],
        ["choice", "Any free text to add to the " "note?", ["Yes", "No"], 0],
        ["input", "Enter free text:", "A gift."],
        [
            "append",
            "Books/What If.md",
            "---\n"
            "tags: book\n"
            "cover: "
            "https://covers/whatif\n"
            "---\n"
            "# What If? Serious Answers\n"
            "author:: [[Randall "
            "Munroe]]\n"
            "year:: 2014\n"
            "publisher:: Houghton\n"
            "recommended_by:: [[Dana]]\n"
            "recommended_by:: [[Omer]]\n"
            "\n"
            "Absurd questions.\n"
            "\n"
            "A gift.",
        ],
        ["say", "Note created in Obsidian!"],
    ],
    "one, confirmed, nothing added": [
        ["input", "Which book do you want to add a note for?", "dune"],
        [
            "choice",
            "Found one book: Dune by Frank Herbert (1965).\n"
            "Is this the one you meant?",
            ["Yes", "No"],
            0,
        ],
        ["choice", 'Add "Recommended by"?', ["Yes", "No"], 1],
        ["choice", "Any free text to add to the note?", ["Yes", "No"], 1],
        [
            "append",
            "Books/Dune.md",
            "---\n"
            "tags: book\n"
            "cover: https://covers/dune\n"
            "---\n"
            "# Dune\n"
            "author:: [[Frank Herbert]]\n"
            "year:: 1965\n"
            "publisher:: Ace\n"
            "\n"
            "Sand.\n"
            "\n",
        ],
        ["say", "Note created in Obsidian!"],
    ],
    "one, not the one": [
        ["input", "Which book do you want to add a note for?", "dune"],
        [
            "choice",
            "Found one book: Dune by Frank Herbert (1965).\n"
            "Is this the one you meant?",
            ["Yes", "No"],
            1,
        ],
        [
            "say",
            "Sorry I couldn't find the book you were looking for. I will "
            "forever live in shame for my failure today.",
        ],
    ],
    "many, cancelled": [
        ["input", "Which book do you want to add a note for?", "dune"],
        [
            "choice",
            "Found multiple books. Which one did you mean?",
            [
                "Dune by Frank Herbert (1965)",
                "Good Omens: The Nice and Accurate Prophecies by Terry "
                "Pratchettand Neil Gaiman (1990)",
                "Design Patterns by Erich Gamma, Richard Helm, Ralph Johnsonand "
                "John Vlissides (1994)",
                "Emma by Jane Austen (Unknown)",
                "Children of Dune by Frank Herbert (1976)",
                "Show more",
                "Cancel",
            ],
            6,
        ],
        [
            "say",
            "Sorry I couldn't find the book you were looking for. I have "
            "brought shame upon my family.",
        ],
    ],
    "none found": [
        ["input", "Which book do you want to add a note for?", "dune"],
        ["say", "No books found. This incident will be reported."],
    ],
    "many, the second, two authors": [
        ["input", "Which book do you want to add a note for?", "omens"],
        [
            "choice",
            "Found multiple books. Which one did you mean?",
            [
                "Dune by Frank Herbert (1965)",
                "Good Omens: The Nice and Accurate Prophecies by "
                "Terry Pratchettand Neil Gaiman (1990)",
                "Design Patterns by Erich Gamma, Richard Helm, "
                "Ralph Johnsonand John Vlissides (1994)",
                "Emma by Jane Austen (Unknown)",
                "Children of Dune by Frank Herbert (1976)",
                "Show more",
                "Cancel",
            ],
            1,
        ],
        ["choice", 'Add "Recommended by"?', ["Yes", "No"], 1],
        ["choice", "Any free text to add to the note?", ["Yes", "No"], 1],
        [
            "append",
            "Books/Good Omens.md",
            "---\n"
            "tags: book\n"
            "cover: https://covers/omens\n"
            "---\n"
            "# Good Omens: The Nice and Accurate Prophecies\n"
            "author:: [[Terry Pratchett]], [[Neil Gaiman]]\n"
            "year:: 1990\n"
            "publisher:: Gollancz\n"
            "\n"
            "An angel and a demon.\n"
            "\n",
        ],
        ["say", "Note created in Obsidian!"],
    ],
}
