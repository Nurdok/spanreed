import asyncio
import datetime
from unittest.mock import AsyncMock, patch

from spanreed.apis.google_books import Book, GoogleBooksPlugin
from spanreed.apis.google_books_blocks import (
    SearchBooks,
    SearchBooksInput,
    google_books_blocks,
)
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.asking import option_text
from spanreed.workflows.blocks import BlockRegistry, RunInfo, StepContext
from spanreed.workflows.testing import fake_plugin


def _book(title: str, authors: list[str], year: int | None = 1965) -> Book:
    date = datetime.date(year, 1, 1) if year else None
    return Book(title, authors, "A publisher", date, "", "")


def test_the_block_is_well_formed() -> None:
    registry = BlockRegistry()
    for block in google_books_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == ["google-books.search-books"]


def test_the_plugin_publishes_the_search_and_has_no_settings() -> None:
    Plugin.reset_registry()
    plugin = GoogleBooksPlugin()

    assert plugin.name() == "Google Books"
    assert plugin.canonical_name() == "google-books"
    assert plugin.has_user_config() is False
    assert [block.id for block in plugin.blocks()] == ["google-books.search-books"]
    # Its blocks carry its name, as those of every plugin do.
    assert all(
        block.id.startswith(plugin.canonical_name() + ".") for block in plugin.blocks()
    )


def test_search_books_returns_what_google_books_finds() -> None:
    found = [_book("Dune", ["Frank Herbert"]), _book("Emma", ["Jane Austen"], None)]
    ctx = StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="run_1", manual=True),
    )

    with patch(
        "spanreed.apis.google_books_blocks.GoogleBooks.get_books",
        new=AsyncMock(return_value=found),
    ) as search:
        output = asyncio.run(SearchBooks().run(ctx, SearchBooksInput(query="dune")))

    search.assert_awaited_once_with(query="dune")
    assert (output.books, output.count) == (found, 2)
    # A book can be an option of "Ask me to choose" as it is.
    assert [option_text(book) for book in output.books] == [
        "Dune by Frank Herbert (1965)",
        "Emma by Jane Austen (Unknown)",
    ]


def test_a_book_in_a_line() -> None:
    assert _book("Dune", ["Frank Herbert"]).label == "Dune by Frank Herbert (1965)"
    assert _book("Good Omens", ["Terry Pratchett", "Neil Gaiman"], 1990).label == (
        "Good Omens by Terry Pratchett and Neil Gaiman (1990)"
    )
    assert _book("Design Patterns", ["A", "B", "C"], 1994).label == (
        "Design Patterns by A, B and C (1994)"
    )
    assert _book("Beowulf", [], None).label == "Beowulf (Unknown)"
