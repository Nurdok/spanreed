import asyncio
import aiohttp
from typing import List, Optional
from dataclasses import dataclass

@dataclass
class Book:
    title: str
    authors: List[str]
    publisher: str
    publish_date: str
    description: str
    thumbnail_url: str

class GoogleBooks:
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, api_key: str):
        self.api_key = api_key
    
    async def get_book(self, query: str) -> Optional[Book]:
        url = f"{self.BASE_URL}?q={query}&key={self.api_key}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                return self._get_book_from_json(data)

    def _get_book_from_json(self, data) -> Optional[Book]:
        items = data.get("items", [])
        if not items:
            return None

        # TODO: decide how to choose book instead of taking the first one.
        volume_info = items[0]["volumeInfo"]

        return Book(
            title=volume_info.get("title", "Unknown"),
            authors=volume_info.get("authors", ["Unknown"]),
            publisher=volume_info.get("publisher", "Unknown"),
            publish_date=volume_info.get("publishedDate", "Unknown"),
            description=volume_info.get("description", ""),
            thumbnail_url=volume_info.get("imageLinks", {}).get("thumbnail", "")
        )
