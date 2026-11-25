"""The Google Books block for workflows."""

from __future__ import annotations

from dataclasses import dataclass

from spanreed.apis.google_books import Book, GoogleBooks
from spanreed.workflows.blocks import Action, Block, StepContext
from spanreed.config_form import described


@dataclass
class SearchBooksInput:
    """What to search for."""

    # A title, an author, or both.
    query: str


@dataclass
class SearchBooksOutput:
    """What the search found, best match first."""

    books: list[Book] = described(
        "The books that were found, best match first. Each has a “label” to "
        "choose it by, so the list can be the options of “Ask me to choose”",
    )
    count: int = described("How many books were found")


class SearchBooks(Action[SearchBooksInput, SearchBooksOutput]):
    """Search Google Books."""

    id = "google-books.search-books"
    title = "Search books"

    async def run(
        self, ctx: StepContext, inputs: SearchBooksInput
    ) -> SearchBooksOutput:
        """Return the books that Google Books finds for the query."""
        books = await GoogleBooks("").get_books(query=inputs.query)
        return SearchBooksOutput(books=books, count=len(books))


def google_books_blocks() -> list[Block]:
    """Return the blocks that the Google Books plugin publishes."""
    return [SearchBooks()]
