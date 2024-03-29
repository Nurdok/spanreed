import pathlib
import base64
import urllib.parse
from typing import List, Optional
import jinja2
from dataclasses import dataclass

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.google_books import GoogleBooks, Book
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.apis.obsidian_webhook import (
    ObsidianWebhookApi,
    ObsidianWebhookPlugin,
)


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
    @classmethod
    def name(cls) -> str:
        return "Lit Notes"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> Optional[type[UserConfig]]:
        return UserConfig

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Add book note", callback=self._ask_for_book_note
            ),
        )

    async def _ask_for_book_note(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        async with bot.user_interaction():
            self._logger.info("Asking for book")
            book: Optional[Book] = await self._ask_for_book(bot)
            if book is None:
                return
            recommended_by: List[str] = await self._ask_for_recommendation(bot)
            free_text: str = await self._ask_for_free_text(bot)

            await self._add_note_for_book(
                user, bot, book, recommended_by, free_text
            )

    async def _ask_for_free_text(self, bot: TelegramBotApi) -> str:
        choice = await bot.request_user_choice(
            "Any free text to add to the note?", ["Yes", "No"]
        )
        if choice == 0:
            return await bot.request_user_input("Enter free text:")
        return ""

    async def _ask_for_recommendation(self, bot: TelegramBotApi) -> List[str]:
        self._logger.info("Asking for recommendation")
        recommended_by: list[str] = []
        while True:
            if recommended_by:
                prompt = 'Add another "Recommended by"?'
            else:
                prompt = 'Add "Recommended by"?'
            choice = await bot.request_user_choice(
                prompt,
                ["Yes", "No"],
            )
            if choice == 1:
                break

            recommended_by.append(
                await bot.request_user_input("Who recommended it?")
            )
        return recommended_by

    async def _ask_for_book(self, bot: TelegramBotApi) -> Optional[Book]:
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
                    "Sorry I couldn't find the book you were looking for. "
                    "I will forever live in shame for my failure today."
                )
                return None

        # More than one book found.
        while True:
            options_to_show = min(5, len(books))
            book_choice = await bot.request_user_choice(
                "Found multiple books. Which one did you mean?",
                [_format_book(book) for book in books[:options_to_show]]
                + ["Show more", "Cancel"],
            )
            if book_choice == options_to_show:  # Show more
                books = books[options_to_show:]
                continue

            if book_choice == options_to_show + 1:  # Cancel
                await bot.send_message(
                    "Sorry I couldn't find the book you were looking for. "
                    "I have brought shame upon my family."
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
    ) -> None:
        user_config: UserConfig = await self.get_config(user)

        env = jinja2.Environment()
        template_params = dict(
            book=book, recommended_by=recommended_by, free_text=free_text
        )
        self._logger.info(f"{template_params=}")

        note_title_template: jinja2.Template = env.from_string(
            user_config.note_title_template
        )
        note_title = note_title_template.render(template_params)

        note_content_template: jinja2.Template = env.from_string(
            user_config.note_content_template
        )
        note_content = note_content_template.render(template_params)
        self._logger.info(f"{note_content=}")

        if await ObsidianWebhookPlugin.is_registered(user):
            await self._create_note_with_obsidian_webhook(
                user, bot, user_config, note_title, note_content
            )
        else:
            await self._send_obsidian_uri(
                bot, user_config, note_title, note_content
            )

    async def _create_note_with_obsidian_webhook(
        self,
        user: User,
        bot: TelegramBotApi,
        user_config: UserConfig,
        note_title: str,
        note_content: str,
    ) -> None:
        self._logger.info("Creating note with Obsidian webhook")
        webhook: ObsidianWebhookApi = await ObsidianWebhookApi.for_user(user)
        await webhook.append_to_note(
            str(
                (
                    pathlib.PurePosixPath(user_config.file_location)
                    / note_title
                ).with_suffix(".md")
            ),
            note_content,
        )

        await bot.send_message(f"Note created in Obsidian!")

    async def _send_obsidian_uri(
        self,
        bot: TelegramBotApi,
        user_config: UserConfig,
        note_title: str,
        note_content: str,
    ) -> None:
        def e(text: str) -> str:
            return urllib.parse.quote(text, safe="")

        obsidian_uri = (
            "https://amir.rachum.com/fwdr?url="
            + base64.urlsafe_b64encode(
                f"obsidian://new?vault={e(user_config.vault)}"
                f"&file={e(user_config.file_location)}{e(note_title)}"
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

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
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
        await cls.set_config(user, user_config)
