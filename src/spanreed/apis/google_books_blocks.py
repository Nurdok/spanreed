"""The Google Books block for workflows."""

from __future__ import annotations

from dataclasses import dataclass

from spanreed.apis.google_books import Book, GoogleBooks
from spanreed.workflows.blocks import Action, Block, StepContext


@dataclass
class SearchBooksInput:
    """What to search for."""

    # A title, an author, or both.
    query: str


@dataclass
class SearchBooksOutput:
    """What the search found, best match first."""

    # Each has a ``label``, a line to choose it by, so the list can be the
    # options of "Ask me to choose". A book also has what a note template
    # of Lit Notes could use: ``short_title``, ``full_title``,
    # ``formatted_authors``, ``formatted_authors_dataview``,
    # ``publication_year`` and ``cover_image_url``.
    books: list[Book]
    count: int


class SearchBooks(Action[SearchBooksInput, SearchBooksOutput]):
    """Search Google Books."""

    id = "lit-notes.search-books"
    title = "Search books"

    async def run(
        self, ctx: StepContext, inputs: SearchBooksInput
    ) -> SearchBooksOutput:
        """Return the books that Google Books finds for the query."""
        books = await GoogleBooks("").get_books(query=inputs.query)
        return SearchBooksOutput(books=books, count=len(books))


def google_books_blocks() -> list[Block]:
    """Return the blocks that the Lit Notes plugin publishes."""
    return [SearchBooks()]
