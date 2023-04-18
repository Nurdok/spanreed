import base64
import urllib.parse
from typing import List, Optional
import jinja2
from dataclasses import dataclass, asdict

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


@dataclass
class UserConfig:
    vault: str
    file_location: str
    note_title_template: str
    note_content_template: str


class LitNotesPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Lit Notes"

    def has_user_config(self) -> bool:
        return True

    async def run(self):
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Add book note", callback=self._ask_for_book_note
            ),
        )

    async def _ask_for_book_note(self, user: User):
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        async with bot.user_interaction():
            book: Optional[Book] = await self._ask_for_book(user, bot)
            if book is None:
                return
            recommended_by: List[str] = await self._ask_for_recommendation(
                user, bot
            )
            free_text: str = await self._ask_for_free_text(user, bot)

            await self._add_note_for_book(
                user, bot, book, recommended_by, free_text
            )

    async def _ask_for_free_text(self, user: User, bot: TelegramBotApi) -> str:
        choice = await bot.request_user_choice(
            "Any free text to add to the note?", ["Yes", "No"]
        )
        if choice == 0:
            return await bot.request_user_input("Go ahead:")
        return ""

    async def _ask_for_recommendation(
        self, user: User, bot: TelegramBotApi
    ) -> List[str]:
        self._logger.info("Asking for recommendation")
        recommended_by = []
        while True:
            choice = await bot.request_user_choice(
                'Add "Recommended by"?',
                ["Yes", "No"],
            )
            if choice == 1:
                break

            recommended_by.append(
                await bot.request_user_input("Who recommended it?")
            )
        return recommended_by

    async def _ask_for_book(
        self, user: User, bot: TelegramBotApi
    ) -> Optional[Book]:
        google_books_api: GoogleBooks = GoogleBooks("")
        book_query = await bot.request_user_input(
            "Which book do you want to add a note for?"
        )
        books: List[Book] = await google_books_api.get_books(query=book_query)

        if not books:
            await bot.send_message(
                "No books found. This incident will be reported."
            )
            return None

        if len(books) == 1:
            book = books[0]
            choice = await bot.request_user_choice(
                f"Found one book: {_format_book(book)}.\n"
                "Is this the one you meant?",
                ["Yes", "No"],
            )
            if choice == 0:
                return book
            else:
                await bot.send_message(
                    "Sorry I couldn't find the book you were looking for."
                )
                return None
        elif len(books) > 1:
            options_to_show = min(5, len(books))
            book_choice = await bot.request_user_choice(
                "Found multiple books. Which one did you mean?",
                [_format_book(book) for book in books[:options_to_show]]
                + ["None of these"],
            )
            if book_choice == options_to_show:
                await bot.send_message(
                    "Sorry I couldn't find the book you were looking for."
                )
                return None
            book = books[book_choice]
            return book

    async def _add_note_for_book(
        self,
        user: User,
        bot: TelegramBotApi,
        book: Book,
        recommended_by: List[str],
        free_text: str,
    ):
        vault = user.config["lit-notes"]["vault"]
        file_location = user.config["lit-notes"]["file_location"]

        env = jinja2.Environment()
        template_params = dict(
            book=book, recommended_by=recommended_by, free_text=free_text
        )
        self._logger.info(f"{template_params=}")

        note_title_template: jinja2.Template = env.from_string(
            user.config["lit-notes"]["note_title_template"]
        )
        note_title = note_title_template.render(template_params)

        note_content_template: jinja2.Template = env.from_string(
            user.config["lit-notes"]["note_content_template"]
        )
        note_content = note_content_template.render(template_params)
        self._logger.info(f"{note_content=}")

        def e(text: str) -> str:
            return urllib.parse.quote(text, safe="")

        obsidian_uri = (
            "https://amir.rachum.com/fwdr?url="
            + base64.urlsafe_b64encode(
                f"obsidian://new?vault={e(vault)}"
                f"&file={e(file_location)}{e(note_title)}"
                f"&content={e(note_content)}".encode()
            ).decode("utf-8")
        )
        message = (
            f'<b><a href="{obsidian_uri}">'
            f"Click here to create the note in Obsidian</a></b>"
        )
        self._logger.info(f"Sending {message=}")
        await bot.send_message(
            text=message,
            parse_html=True,
        )

    async def ask_for_user_config(self, user: User):
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        # No need to acquire the user interaction lock here, since we're
        # being called by a method that has it already.
        vault = await bot.request_user_input(
            "What is the name of the Obsidian vault you want to use?"
        )
        file_location = await bot.request_user_input(
            "What is the file location of the note you want to create?"
        )
        note_title_template = await bot.request_user_input(
            "What is the template for the note title?"
        )
        note_content_template = await bot.request_user_input(
            "What is the template for the note content?"
        )

        user_config = UserConfig(
            vault=vault,
            file_location=file_location,
            note_title_template=note_title_template,
            note_content_template=note_content_template,
        )
        await user.set_config_for_plugin(
            self.canonical_name, asdict(user_config)
        )
