import datetime
import logging

import aiohttp
from typing import List, Optional, NamedTuple, Tuple
from dataclasses import dataclass
import re
import dateutil.parser


@dataclass
class Book:
    title: str
    authors: Tuple[str]
    publisher: str
    publication_date: datetime.date
    description: str
    thumbnail_url: str

    @property
    def short_title(self) -> str:
        unsupported_characters = r"""[*"\/\\<>:|?]+"""
        return re.split(unsupported_characters, self.title)[0]

    @property
    def formatted_authors(self) -> str:
        return ", ".join(f"[[{author}]]" for author in self.authors)

    @property
    def publication_year(self) -> str:
        if self.publication_date is None:
            return "Unknown"
        return str(self.publication_date.year)


def parse_date(date_str: Optional[str]) -> Optional[datetime.date]:
    if date_str is None:
        return None

    try:
        return dateutil.parser.parse(date_str).date()
    except (ValueError, dateutil.parser.ParserError):
        return None


class GoogleBooks:
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_books(self, query: str) -> List[Book]:
        url = f"{self.BASE_URL}?q={query}&key={self.api_key}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                return self._get_books_from_json(data)

    def _get_books_from_json(self, data) -> List[Book]:
        items = data.get("items", [])
        if not items:
            return None

        # TODO: decide how to choose book instead of taking the first one.
        books = []
        for item in items:
            volume_info = item["volumeInfo"]

            logging.getLogger(__name__).info(volume_info)
            books.append(
                Book(
                    title=volume_info.get("title", "Unknown"),
                    authors=tuple(volume_info.get("authors", ["Unknown"])),
                    publisher=volume_info.get("publisher", "Unknown"),
                    publication_date=parse_date(
                        volume_info.get("publishedDate", None)
                    ),
                    description=volume_info.get("description", ""),
                    thumbnail_url=volume_info.get("imageLinks", {}).get(
                        "thumbnail", ""
                    ),
                )
            )

        return books
