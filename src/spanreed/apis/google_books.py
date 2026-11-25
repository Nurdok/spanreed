"""Google Books: the search for a book, and the plugin that publishes it."""

import datetime
import logging

import aiohttp
from typing import TYPE_CHECKING, Optional, Any
from dataclasses import dataclass
import re
import dateutil.parser

from spanreed.plugin import Plugin
from spanreed.config_form import described

if TYPE_CHECKING:
    from spanreed.workflows.blocks import Block


@dataclass
class Book:
    title: str = described("The title of the book")
    authors: list[str] = described("Who wrote it")
    publisher: str = described("Who published it")
    publication_date: Optional[datetime.date] = described(
        "The day it was published. Nothing if Google Books has none",
    )
    description: str = described("What Google Books says the book is about")
    thumbnail_url: str = described("The address of a picture of the cover")

    # For compatibility with Booksidian.
    @property
    def full_title(self) -> str:
        return self.title

    # For Obsidian compatibility.
    @property
    def short_title(self) -> str:
        unsupported_characters = r"""[*"\/\\<>:|?]+"""
        return re.split(unsupported_characters, self.title)[0]

    @property
    def formatted_authors(self) -> str:
        return ", ".join(f"[[{author}]]" for author in self.authors)

    @property
    def formatted_authors_dataview(self) -> str:
        return ", ".join(f"(author:: [[{author}]])" for author in self.authors)

    @property
    def publication_year(self) -> str:
        if self.publication_date is None:
            return "Unknown"
        return str(self.publication_date.year)

    # For compatibility with Booksidian.
    @property
    def cover_image_url(self) -> str:
        return self.thumbnail_url

    @property
    def label(self) -> str:
        """Return the book in a line, to choose it by: title, authors, year."""
        by = ""
        if len(self.authors) == 1:
            by = f" by {self.authors[0]}"
        elif self.authors:
            by = f" by {', '.join(self.authors[:-1])} and {self.authors[-1]}"
        return f"{self.title}{by} ({self.publication_year})"


def parse_date(date_str: Optional[str]) -> Optional[datetime.date]:
    if date_str is None:
        return None

    try:
        return dateutil.parser.parse(date_str).date()
    except (ValueError, dateutil.parser.ParserError):
        return None


class GoogleBooksPlugin(Plugin):
    """The connection to Google Books: its search, as a block for workflows.

    It has no settings and runs nothing. It took the place of Lit Notes,
    which wrote a note for a book. The recipe "Add book note" does that
    now, and this search was what was left of the plugin (see
    ``spanreed.workflows.migrations``).
    """

    @classmethod
    def name(cls) -> str:
        return "Google Books"

    def blocks(self) -> list["Block"]:
        """Return the book search for workflows."""
        # Imported here: the blocks import this module for ``GoogleBooks``.
        from spanreed.apis.google_books_blocks import google_books_blocks

        return google_books_blocks()


class GoogleBooks:
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_books(self, query: str) -> list[Book]:
        url = f"{self.BASE_URL}?q={query}&key={self.api_key}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                return self._get_books_from_json(data)

    def _get_books_from_json(self, data: dict[str, Any]) -> list[Book]:
        items = data.get("items", [])
        if not items:
            return []

        books = []
        for item in items:
            volume_info = item["volumeInfo"]

            logging.getLogger(__name__).info(volume_info)
            full_title = volume_info.get("title", "Unknown")
            if "subtitle" in volume_info:
                full_title += f": {volume_info['subtitle']}"

            books.append(
                Book(
                    title=full_title,
                    authors=list(volume_info.get("authors", ["Unknown"])),
                    publisher=volume_info.get("publisher", "Unknown"),
                    publication_date=parse_date(volume_info.get("publishedDate", None)),
                    description=volume_info.get("description", ""),
                    thumbnail_url=volume_info.get("imageLinks", {}).get(
                        "thumbnail", ""
                    ),
                )
            )

        return books
