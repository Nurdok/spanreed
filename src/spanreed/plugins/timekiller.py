import re
import json
import asyncio
import pathlib
import random
import datetime
import textwrap
from dataclasses import dataclass

from spanreed.apis.telegram_bot import (
    TelegramBotApi,
    PluginCommand,
    UserInteractionPreempted,
)
from spanreed.apis.obsidian_webhook import ObsidianWebhookApi
from spanreed.apis.obsidian import ObsidianApi
from spanreed.user import User
from spanreed.plugin import Plugin
from spanreed.plugins.spanreed_monitor import suppress_and_log_exception


@dataclass
class UserConfig:
    # Heading in the daily note under which journaling answers are added. When
    # None (the default), answers are appended to the end of the note.
    journal_heading: str | None = None


class TimekillerPlugin(Plugin):
    LAST_ASKED_BOOKS_KEY = "currently-reading-books-last-asked"
    SKIPPED_SCANS_KEY = "skipped-scans"

    @classmethod
    def name(cls) -> str:
        return "Timekiller"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        heading = (
            await bot.request_user_input(
                "Under which heading should journaling answers be added in the"
                " daily note?\nEnter the heading text (e.g. 'Journal'), or"
                " 'none' to append them at the end of the note."
            )
        ).strip()
        await cls.set_config(
            user,
            UserConfig(
                journal_heading=None
                if heading == "" or heading.lower() == "none"
                else heading
            ),
        )

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Kill time",
                callback=self._kill_time,
            ),
        )

        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Clear skipped scans",
                callback=self._clear_skipped_scans_command,
            ),
        )

        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Set journal heading",
                callback=self._set_journal_heading_command,
            ),
        )

        await super().run()

    async def _set_journal_heading_command(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        current: str = config.journal_heading or "(none — appended at the end)"
        heading = (
            await bot.request_user_input(
                f"Current journal heading: {current}\n\n"
                "Enter a new heading for journaling answers, or 'none' to"
                " append them at the end of the note:"
            )
        ).strip()
        config.journal_heading = (
            None if heading == "" or heading.lower() == "none" else heading
        )
        await self.set_config(user, config)
        if config.journal_heading is None:
            await bot.send_message(
                "Journaling answers will be appended to the end of the note."
            )
        else:
            await bot.send_message(
                f"Journaling answers will be added under “{config.journal_heading}”."
            )

    async def _get_skipped_scans(self, user: User) -> set[str]:
        raw = await self.get_user_data(user, self.SKIPPED_SCANS_KEY)
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return set()

    async def _add_skipped_scan(self, user: User, path: str) -> None:
        skipped = await self._get_skipped_scans(user)
        skipped.add(path)
        await self.set_user_data(
            user, self.SKIPPED_SCANS_KEY, json.dumps(sorted(skipped))
        )

    async def _clear_skipped_scans(self, user: User) -> None:
        await self.set_user_data(user, self.SKIPPED_SCANS_KEY, json.dumps([]))

    async def _clear_skipped_scans_command(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        skipped = await self._get_skipped_scans(user)
        if not skipped:
            await bot.send_message("No skipped scans to clear.")
            return
        await self._clear_skipped_scans(user)
        await bot.send_message(
            f"Cleared {len(skipped)} skipped scan(s); I'll ask about them again."
        )

    async def run_for_user(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        while True:
            now: datetime.datetime = datetime.datetime.now()
            if now.hour > 7 or now.hour < 22:
                async with suppress_and_log_exception(
                    TimeoutError, UserInteractionPreempted
                ):
                    async with bot.user_interaction():
                        await self._kill_time_push(user)
            await asyncio.sleep(
                datetime.timedelta(hours=random.randrange(1, 3)).total_seconds()
            )

    async def get_available_time_killers(
        self,
        user: User,
        obsidian: ObsidianApi,
        push: bool,
    ) -> dict:
        timekillers: dict = {
            "Journaling Prompt": self._journal_prompt,
            "Scan Processing": self.prompt_for_scan_processing,
        }

        last_asked: datetime.datetime = datetime.datetime.now() - datetime.timedelta(
            days=4
        )
        last_asked_str: str | None = await self.get_user_data(
            user, self.LAST_ASKED_BOOKS_KEY
        )
        if last_asked_str is not None:
            try:
                last_asked = datetime.datetime.fromisoformat(last_asked_str)
            except TypeError:
                raise ValueError(f"{last_asked_str}, {type(last_asked_str)}")
        if not push or datetime.datetime.now() - last_asked > datetime.timedelta(
            days=3
        ):
            timekillers["Books"] = self.prompt_for_currently_reading_books

        await obsidian.safe_generate_today_note()
        daily_note: str = await obsidian.get_daily_note("Daily")
        if await obsidian.get_property(daily_note, "mood") is None:
            timekillers["Mood"] = self._poll_for_metrics

        return timekillers

    async def _kill_time_push(self, user: User) -> None:
        """Ask the user to kill time without provocation.

        Skips questions about what killtime activity to do to reduce friction.
        """
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        timekillers: dict = await self.get_available_time_killers(
            user,
            obsidian,
            True,
        )
        choice: str = random.choice(list(timekillers.keys()))
        await timekillers[choice](user, bot, obsidian)

    async def _kill_time(self, user: User) -> None:
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        timekillers: dict = await self.get_available_time_killers(
            user,
            obsidian,
            False,
        )
        choices: list[str] = [name for name in timekillers.keys()]
        choice: int = await bot.request_user_choice("What's your poison?", choices)
        await timekillers[choices[choice]](user, bot, obsidian)

    async def _journal_prompt(
        self, user: User, bot: TelegramBotApi, obsidian: ObsidianApi
    ) -> None:
        prompts: list[str] = [
            "What are you doing right now?",
            "What are you grateful for today?",
            "What are you looking forward to today?",
            "What are you struggling with today?",
            "What are you proud of today?",
            "What are you excited about today?",
            "What are you worried about today?",
            "Did you make progress on a project today? If so, what?",
            "What is one thing you learned today?",
            "What did you do today to take care of yourself?",
            "What did you do today to take care of someone else?",
            "What did you do today to take care of your home?",
            "What friend did you talk to today?",
            "What family member did you talk to today?",
            "What did you do today for your yearly theme?",
            "Take a picture of you or something you did today.",
            "What did you do for physical health today?",
            "What did you do for mental health today?",
            "What did you do today for fun?",
            "What are you currently watching?",
            "What are you currently listening to?",
            "What is your favorite song right now?",
            "Do you have plans to meet up with friends anytime soon?",
            "What project are you currently working on?",
        ]

        config: UserConfig = await self.get_config(user)
        date_str: str = datetime.datetime.today().strftime("%Y-%m-%d")
        note_name: str = f"Daily/{date_str}.md"

        while True:
            prompt = random.choice(prompts)
            choices = ["Answer", "Change", "Cancel"]
            choice: int = await bot.request_user_choice(
                f"Prompt: {prompt}", choices, columns=3
            )
            if choices[choice] == "Cancel":
                break
            elif choices[choice] == "Change":
                continue

            prompt_answer: str = await bot.request_user_input(prompt)
            note_content: str = f"\n\n### {prompt}\n{prompt_answer}\n"
            self._logger.info(f"Appending to note {note_name}")
            await obsidian.append_to_note(
                note_name, note_content, heading=config.journal_heading
            )
            await bot.send_message("Noted!")
            if (
                await bot.request_user_choice("Another?", ["Yes", "No"], columns=2)
            ) == 1:
                break

    async def _poll_for_metrics(
        self, user: User, bot: TelegramBotApi, obsidian: ObsidianApi
    ) -> None:
        daily_note: str = await obsidian.get_daily_note("Daily")
        if await obsidian.get_property(daily_note, "mood") is not None:
            await bot.send_message("You've already recorded your mood today.")
            return

        mood_choices = ["1", "2", "3", "4", "5", "Cancel"]
        mood_choice: int = await bot.request_user_choice(
            "How would you rate your mood right now?\n" " (1 - negative, 5 - positive)",
            mood_choices,
            columns=5,
        )

        if mood_choice == len(mood_choices) - 1:
            return

        mood: int = mood_choice + 1

        possible_feelings: list[str] = [
            "happy",
            "sad",
            "angry",
            "depressed",
            "anxious",
            "excited",
            "tired",
            "energetic",
            "bored",
            "stressed",
            "calm",
            "confused",
            "frustrated",
            "grateful",
            "proud",
            "lonely",
            "loved",
            "motivated",
            "optimistic",
            "pessimistic",
            "relaxed",
            "restless",
            "satisfied",
            "scared",
            "shocked",
            "sick",
            "sore",
            "stressed",
            "surprised",
            "thankful",
            "uncomfortable",
            "worried",
            "focused",
        ]
        feelings: list[str] = []

        while True:
            feeling_choice: int = await bot.request_user_choice(
                "What are you feeling right now?",
                possible_feelings + ["Done"],
                columns=3,
            )
            if feeling_choice == len(possible_feelings):
                break
            feelings.append(possible_feelings[feeling_choice])

        await obsidian.safe_generate_today_note()
        # TODO: Use ObsidianApi to get the daily note path.
        await obsidian.set_value_of_property(daily_note, "mood", str(mood))
        await obsidian.set_value_of_property(daily_note, "feelings", feelings)
        await bot.send_message("Noted!")

    async def prompt_for_currently_reading_books(
        self, user: User, bot: TelegramBotApi, obsidian: ObsidianApi
    ) -> None:
        books = await obsidian.query_dataview(
            """
        table title
        from #book
        where status = "reading"
        """
        )
        if not books:
            return

        for book in books:
            mark_as_finished: bool = False
            choice = await bot.request_user_choice(
                f'How\'s it going with reading "{book.title}"?',
                [
                    "No notes (next)",
                    "I have notes",
                    "Finished!",
                    "Giving up",
                    "Cancel",
                ],
                columns=2,
            )
            if choice == 0:
                continue
            if choice == 4:
                break
            if choice == 2:
                mark_as_finished = True
            if choice == 3:
                mark_as_finished = (
                    await bot.request_user_choice(
                        "Do you want to mark it as finished?",
                        ["Yes", "No"],
                    )
                    == 0
                )
            if mark_as_finished:
                await obsidian.set_value_of_property(
                    book.file["path"], "status", "read"
                )
                finish_date_choice: int = await bot.request_user_choice(
                    "When did you finish it?",
                    [
                        "Today",
                        "Yesterday",
                        "Other (specify)",
                        "Other (skip)",
                    ],
                )
                finish_date: datetime.date | None = None
                if finish_date_choice == 0:
                    finish_date = datetime.date.today()
                elif finish_date_choice == 1:
                    finish_date = datetime.date.today() - datetime.timedelta(days=1)
                elif finish_date_choice == 2:
                    finish_date = datetime.datetime.strptime(
                        await bot.request_user_input(
                            "When did you finish it?\n" "Use the format YYYY-MM-DD."
                        ),
                        "%Y-%m-%d",
                    ).date()
                if finish_date is not None:
                    await obsidian.set_value_of_property(
                        book.file["path"],
                        "finish-date",
                        finish_date.strftime("%Y-%m-%d"),
                    )
            if choice == 1:
                obsidian_webhook: ObsidianWebhookApi = (
                    await ObsidianWebhookApi.for_user(user)
                )
                await obsidian_webhook.append_to_note(
                    book.file["path"],
                    "\n\n### Thoughts\n"
                    + await bot.request_user_input("Go ahead then:"),
                )
        await self.set_user_data(
            user,
            self.LAST_ASKED_BOOKS_KEY,
            datetime.datetime.now().isoformat(),
        )

    async def prompt_for_scan_processing(
        self, user: User, bot: TelegramBotApi, obsidian: ObsidianApi
    ) -> None:
        iso8601_date_pattern = re.compile(r"\d{4}-\d{2}-\d{2}")
        pattern = re.compile(r"\d{4}-\d{2}-\d{2} Scan")
        # TODO: Replace with user config
        base_path: str = "Assets/scans"
        scans = [
            pathlib.PurePosixPath(path) for path in await obsidian.list_dir(base_path)
        ]
        unprocessed_scans = [
            scan for scan in scans if pattern.search(scan.name) is not None
        ]
        skipped = await self._get_skipped_scans(user)
        unprocessed_pdfs = [
            scan
            for scan in unprocessed_scans
            if scan.suffix == ".pdf" and str(scan) not in skipped
        ]
        if not unprocessed_pdfs:
            return
        pdf_file = unprocessed_pdfs[0]
        pdf_bytes: bytes = await obsidian.read_binary_file(str(pdf_file))
        await bot.send_document(pdf_file.name, pdf_bytes)

        action = await bot.request_user_choice(
            f"What do you want to do with “{pdf_file.name}”?",
            ["Rename", "Delete", "Skip (don't ask again)", "Cancel"],
            columns=2,
        )
        if action == 3:  # Cancel
            return
        if action == 2:  # Skip (don't ask again)
            await self._add_skipped_scan(user, str(pdf_file))
            await bot.send_message(
                f"Okay, I won't ask about “{pdf_file.name}” again. "
                "Use “Clear skipped scans” to reset."
            )
            return
        if action == 1:  # Delete
            if (
                await bot.request_user_choice(
                    f"Delete “{pdf_file.name}”? It will be moved to the trash.",
                    ["Yes, delete", "No"],
                )
                == 0
            ):
                await obsidian.delete_file(str(pdf_file))
                await bot.send_message(f"Deleted “{pdf_file.name}” (moved to trash).")
            return

        # action == 0: Rename
        existing_date = pdf_file.stem[:10]
        new_date: str = ""
        # Some errors will require doing the whole interaction again
        while True:
            date_choice = await bot.request_user_choice(
                f"Is the existing date ({existing_date}) okay?",
                ["Yes (keep)", "No (change)", "Cancel"],
                columns=2,
            )
            if date_choice == 2:
                return
            if date_choice == 1:
                while iso8601_date_pattern.match(new_date) is None:
                    new_date = await bot.request_user_input(
                        "Enter a date (in ISO-8601 format, YYYY-MM-DD) for the scan:"
                    )
            new_name: str = await bot.request_user_input(
                "Enter a new name for the scan:"
            )
            new_path = pathlib.PurePosixPath(
                f"Assets/scans/{new_date or existing_date} {new_name}.pdf"
            )
            if (
                await bot.request_user_choice(
                    f"Rename {pdf_file.name} to {new_path.name}?",
                    ["Yes", "No"],
                )
                == 0
            ):
                try:
                    await obsidian.move_file(str(pdf_file), str(new_path))
                except FileExistsError:
                    await bot.send_message(f'File "{new_path.name}" already exists.')
                    if (
                        await bot.request_user_choice(
                            "Do you want to enter a new name?", ["Yes", "No"]
                        )
                    ) == 1:
                        return
                else:
                    await bot.send_message("Moved!")
                    return
