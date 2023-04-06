from typing import List

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.google_books import GoogleBooks, Book
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand


def _format_book(book: Book) -> str:
    if not book.authors:
        return book.title
    if len(book.authors) == 1:
        return f"{book.title} by {book.authors[0]}"
    else:
        return (
            f"{book.title} by {', '.join(book.authors[:-1])} "
            "and {book.authors[-1]}"
        )


class LitNotesPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Lit Notes"

    async def run(self):
        await TelegramBotApi.register_command(
            self,
            PluginCommand(text="Add book note", callback=self.ask_for_book),
        )

    async def ask_for_book(self, user: User):
        bot: TelegramBotApi = TelegramBotApi.for_user(self._user)
        google_books_api: GoogleBooks = GoogleBooks.for_user(user)

        book_query = await bot.request_user_input(
            "Which book do you want to add a note for?"
        )
        books: List[Book] = await google_books_api.get_books(query=book_query)

        if not books:
            await bot.send_message(
                "No books found. This incident will be reported."
            )
            return

        if len(books) == 1:
            book = books[0]
            choice = await bot.request_user_choice(
                f"Found one book: {_format_book(book)}.\n"
                "Is this the one you meant?",
                ["Yes", "No"],
            )
            if choice == 0:
                await self.add_note_for_book(book, user)
        else:
            book_choice = await bot.request_user_choice(
                "Found multiple books. Which one did you mean?",
                [_format_book(book) for book in books[:5]] + ["None of these"],
            )
            book = books[book_choice]
            await self.add_note_for_book(book, user)

    async def add_note_for_book(self, book: Book, user: User):
        bot: TelegramBotApi = TelegramBotApi.for_user(self._user)
        await bot.send_message(
            f"Adding note for {_format_book(book)}... jk not implemented"
        )