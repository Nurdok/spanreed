import asyncio
import random
import datetime

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


class TimekillerPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Timekiller"

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Kill time",
                callback=self._kill_time,
            ),
        )

        await super().run()

    async def run_for_user(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        while True:
            await asyncio.sleep(
                datetime.timedelta(
                    hours=random.randrange(3, 7)
                ).total_seconds()
            )
            now: datetime.datetime = datetime.datetime.now()
            if now.hour < 9 or now.hour > 22:
                continue

            async with suppress_and_log_exception(
                TimeoutError, UserInteractionPreempted
            ):
                async with bot.user_interaction():
                    if (
                        await bot.request_user_choice(
                            "Got some time to kill?",
                            ["Yes", "No"],
                        )
                        != 0
                    ):
                        continue
                    await self._kill_time(user)

    async def _kill_time(self, user: User) -> None:
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        choices: list[str] = ["Mood", "Journaling Prompt", "Books", "Cancel"]
        choice: int = await bot.request_user_choice(
            "What's your poison?", choices
        )
        if choices[choice] == "Mood":
            await self._poll_for_metrics(user, bot, obsidian)
        elif choices[choice] == "Journaling Prompt":
            await self._journal_prompt(user, bot)
        elif choices[choice] == "Books":
            await self.prompt_for_currently_reading_books(user, bot, obsidian)

    async def _journal_prompt(self, user: User, bot: TelegramBotApi) -> None:
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
            "What book are you currently reading?",
            "What are you currently watching?",
            "What are you currently listening to?",
            "What is your favorite song right now?",
            "Do you have plans to meet up with friends anytime soon?",
            "What project are you currently working on?",
        ]

        webhook_api: ObsidianWebhookApi = await ObsidianWebhookApi.for_user(
            user
        )
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
            await webhook_api.append_to_note(note_name, note_content)
            await bot.send_message("Noted!")
            if (
                await bot.request_user_choice(
                    "Another?", ["Yes", "No"], columns=2
                )
            ) == 1:
                break

    async def _poll_for_metrics(
        self, user: User, bot: TelegramBotApi, obsidian: ObsidianApi
    ) -> None:
        # TODO: Use ObsidianApi to check if we've already done this today.
        mood: int = (
            await bot.request_user_choice(
                "How would you rate your mood right now?\n"
                " (1 - negative, 5 - positive)",
                ["1", "2", "3", "4", "5"],
                columns=5,
            )
            + 1
        )

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
            choice: int = await bot.request_user_choice(
                "What are you feeling right now?",
                possible_feelings + ["Done"],
                columns=3,
            )
            if choice == len(possible_feelings):
                break
            feelings.append(possible_feelings[choice])

        await obsidian.safe_generate_today_note()
        # TODO: Use ObsidianApi to get the daily note path.
        daily_note: str = await obsidian.get_daily_note("Daily")
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
            if (
                await bot.request_user_choice(
                    f'Are you still reading "{book.title}"?',
                    ["Yes", "No"],
                )
                == 1
            ):
                mark_as_finished: bool = False
                finished_choice = await bot.request_user_choice(
                    "Why?",
                    ["Finished", "Stopped reading"],
                )
                if finished_choice == 0:
                    mark_as_finished = True
                if finished_choice == 1:
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
                        finish_date = (
                            datetime.date.today() - datetime.timedelta(days=1)
                        )
                    elif finish_date_choice == 2:
                        finish_date = datetime.datetime.strptime(
                            await bot.request_user_input(
                                "When did you finish it?\n"
                                "Use the format YYYY-MM-DD."
                            ),
                            "%Y-%m-%d",
                        ).date()
                    if finish_date is not None:
                        await obsidian.set_value_of_property(
                            book.file["path"],
                            "finish-date",
                            finish_date.strftime("%Y-%m-%d"),
                        )
            if (
                await bot.request_user_choice(
                    "Any thoughts you want to record?", ["Yes", "No"]
                )
                == 0
            ):
                obsidian_webhook: ObsidianWebhookApi = (
                    await ObsidianWebhookApi.for_user(user)
                )
                await obsidian_webhook.append_to_note(
                    book.file["path"],
                    "\n\n### Thoughts\n"
                    + await bot.request_user_input("Go ahead then:"),
                )
