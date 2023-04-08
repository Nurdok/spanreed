import urllib.parse
from typing import List
import re
import jinja2

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.google_books import GoogleBooks, Book
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand


def _format_book(book: Book) -> str:
    title: str = book.title
    authors: str = ""
    if book.authors:
        authors = f" by "
        if len(book.authors) == 1:
            authors += book.authors[0]
        else:
            authors += ", ".join(book.authors[:-1]) + f"and {book.authors[-1]}"
    return f"{title}{authors} ({book.publication_year})"


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
        self._logger.info("Asking for book")
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        google_books_api: GoogleBooks = GoogleBooks("")

        self._logger.info("Asking for user input...")

        async with bot.user_interaction():
            book_query = await bot.request_user_input(
                "Which book do you want to add a note for?"
            )
            books: List[Book] = await google_books_api.get_books(
                query=book_query
            )

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
                    await bot.send_message("Sorry I couldn't help.")
            elif len(books) > 1:
                book_choice = await bot.request_user_choice(
                    "Found multiple books. Which one did you mean?",
                    [_format_book(book) for book in books[:5]]
                    + ["None of these"],
                )
                book = books[book_choice]
                await self.add_note_for_book(book, user)
                if book_choice == len(books):
                    await bot.send_message("Sorry I couldn't help.")

            # TODO: make this configurable
            vault = user.config["lit-notes"]["vault"]
            file_location = user.config["lit-notes"]["file_location"]

            unsupported_characters = r"""[*"\/\\<>:|?]+"""
            short_title = re.split(unsupported_characters, book.title)[0]

            env = jinja2.Environment()

            note_title_template: jinja2.Template = env.from_string(
                user.config["lit-notes"]["note_title_template"]
            )
            note_title = note_title_template.render(book=book)

            note_content_template: jinja2.Template = env.from_string(
                user.config["lit-notes"]["note_content_template"]
            )
            note_content = note_content_template.render(book=book)

            def e(text: str) -> str:
                return urllib.parse.quote(text, safe="")

            obsidian_uri = "https://amir.rachum.com/fwdr?url=" + e(
                f"obsidian://new?vault={e(vault)}"
                f"&file={e(file_location)}{e(note_title)}"
                f"&content={e(note_content)}"
            )
            message = f'<b><a href="{obsidian_uri}">Open in Obsidian</a></b>'
            self._logger.info(f"Sending {message=}")
            await bot.send_message(
                text=message,
                parse_html=True,
            )

    async def add_note_for_book(self, book: Book, user: User):
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        await bot.send_message(
            f"Adding note for {_format_book(book)}... jk not implemented"
        )
