import asyncio
import datetime
import difflib
import re
from typing import Any

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.audible import AudibleApi, AudibleBook, AudiblePlugin
from spanreed.apis.obsidian import ObsidianApi, ObsidianPlugin
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand


SYNC_INTERVAL = datetime.timedelta(hours=1)
LINK_SNOOZE_DURATION = datetime.timedelta(hours=24)
FINISH_THRESHOLD_PERCENT = 99
MATCH_SCORE_CUTOFF = 0.5
MAX_MATCH_CANDIDATES = 3
# Telegram inline keyboard buttons get visually truncated around this length.
MAX_CHOICE_LABEL_LENGTH = 60


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _match_score(note_title: str, book: AudibleBook) -> float:
    """Similarity between a note title and an Audible book title.

    Audible titles often carry series suffixes after a colon (e.g.
    "The Way of Kings: The Stormlight Archive, Book 1"), so the main part
    before the colon is scored as well and the best score wins.
    """
    note = _normalize_title(note_title)
    scores = []
    for candidate in (book.title, book.title.split(":")[0]):
        scores.append(
            difflib.SequenceMatcher(None, note, _normalize_title(candidate)).ratio()
        )
    return max(scores)


def find_match_candidates(
    note_title: str, books: list[AudibleBook]
) -> list[AudibleBook]:
    scored = [(book, _match_score(note_title, book)) for book in books]
    matches = [(book, score) for book, score in scored if score >= MATCH_SCORE_CUTOFF]
    matches.sort(key=lambda pair: pair[1], reverse=True)
    return [book for book, _ in matches[:MAX_MATCH_CANDIDATES]]


class BookProgressPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Audible Book Progress"

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [AudiblePlugin, ObsidianPlugin]

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(text="Sync book progress", callback=self._sync_now),
        )
        await super().run()

    async def run_for_user(self, user: User) -> None:
        while True:
            await self._sync(user)
            await asyncio.sleep(SYNC_INTERVAL.total_seconds())

    async def _sync_now(self, user: User) -> None:
        """Manually triggered sync via the Telegram command."""
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        await bot.notify("Syncing Audible book progress…")
        updated = await self._sync(user)
        if updated == 0:
            await bot.notify("No book progress changes found.")

    async def _sync(self, user: User) -> int:
        """Sync Audible progress onto currently-reading book notes.

        Returns the number of notes whose progress was updated.
        """
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        notes = await obsidian.query_dataview(
            """
        table title, asin
        from #book
        where status = "reading"
        """
        )
        if not notes:
            return 0

        audible_api: AudibleApi = await AudibleApi.for_user(user)
        library: list[AudibleBook] = await audible_api.get_library()
        library_by_asin = {book.asin: book for book in library}

        updated = 0
        for note in notes:
            note_path: str = note.file["path"]
            book: AudibleBook | None
            if note.asin:
                book = library_by_asin.get(note.asin)
                if book is None:
                    self._logger.warning(
                        f"Note {note_path} has asin {note.asin} that isn't"
                        " in the Audible library; skipping."
                    )
                    continue
            else:
                book = await self._link_note_to_book(user, obsidian, note, library)
                if book is None:
                    continue

            if await self._update_progress(obsidian, note_path, book):
                updated += 1
            await self._maybe_prompt_finished(user, obsidian, note_path, book)
        return updated

    def _note_title(self, note: Any) -> str:
        if note.title:
            return str(note.title)
        # Fall back to the note's filename.
        note_path: str = note.file["path"]
        return note_path.rsplit("/", 1)[-1].removesuffix(".md")

    async def _link_note_to_book(
        self,
        user: User,
        obsidian: ObsidianApi,
        note: Any,
        library: list[AudibleBook],
    ) -> AudibleBook | None:
        """Fuzzy-match a note to an Audible book, verified by the user.

        The chosen ASIN is persisted on the note so matching is exact from
        then on. Books marked "Not on Audible" are never asked about again;
        "Ask me later" snoozes the question for a day.
        """
        note_path: str = note.file["path"]
        if await self.get_user_data(user, f"not-audible:{note_path}"):
            return None
        snoozed_at: str | None = await self.get_user_data(
            user, f"link-snooze:{note_path}"
        )
        if snoozed_at is not None and (
            datetime.datetime.now()
            < datetime.datetime.fromisoformat(snoozed_at) + LINK_SNOOZE_DURATION
        ):
            return None

        title = self._note_title(note)
        candidates = find_match_candidates(title, library)
        if not candidates:
            self._logger.info(f"No Audible match candidates for {note_path}; snoozing.")
            await self._snooze_link(user, note_path)
            return None

        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        async with bot.user_interaction():
            choice = await bot.request_user_choice(
                f'Which Audible book is "{title}"?',
                [book.title[:MAX_CHOICE_LABEL_LENGTH] for book in candidates]
                + ["Not on Audible", "Ask me later"],
            )
        if choice < len(candidates):
            book = candidates[choice]
            await obsidian.set_value_of_property(note_path, "asin", book.asin)
            return book
        if choice == len(candidates):  # Not on Audible
            await self.set_user_data(user, f"not-audible:{note_path}", "true")
        else:  # Ask me later
            await self._snooze_link(user, note_path)
        return None

    async def _snooze_link(self, user: User, note_path: str) -> None:
        await self.set_user_data(
            user,
            f"link-snooze:{note_path}",
            datetime.datetime.now().isoformat(),
        )

    async def _update_progress(
        self, obsidian: ObsidianApi, note_path: str, book: AudibleBook
    ) -> bool:
        progress = round(book.percent_complete)
        current = await obsidian.get_property(note_path, "progress")
        if current == progress:
            return False
        await obsidian.set_value_of_property(note_path, "progress", progress)
        return True

    async def _maybe_prompt_finished(
        self,
        user: User,
        obsidian: ObsidianApi,
        note_path: str,
        book: AudibleBook,
    ) -> None:
        """Offer (once) to mark a note as read when Audible says it's done."""
        if book.percent_complete < FINISH_THRESHOLD_PERCENT and not book.is_finished:
            return
        if await self.get_user_data(user, f"finish-prompted:{book.asin}"):
            return

        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        async with bot.user_interaction():
            choice = await bot.request_user_choice(
                f'Looks like you finished "{book.title}" on Audible.\n'
                "Mark it as read?",
                ["Yes", "No"],
            )
        await self.set_user_data(
            user,
            f"finish-prompted:{book.asin}",
            datetime.datetime.now().isoformat(),
        )
        if choice == 0:
            await obsidian.set_value_of_property(note_path, "status", "read")
            await obsidian.set_value_of_property(
                note_path,
                "finish-date",
                datetime.date.today().strftime("%Y-%m-%d"),
            )
