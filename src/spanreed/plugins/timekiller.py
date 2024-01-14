import asyncio
import random
import datetime

from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.apis.obsidian_webhook import ObsidianWebhookApi
from spanreed.apis.obsidian import ObsidianApi
from spanreed.plugins.habit_tracker import (
    EventStorageRedis,
    ActivityType,
    Event,
    EventType,
)
from spanreed.user import User
from spanreed.plugin import Plugin


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

            async with bot.user_interaction():
                if (
                    await bot.request_user_choice(
                        "Got some time to kill?",
                        ["Yes", "No"],
                    )
                    == 0
                ):
                    await self._poll_for_metrics(user, bot)
                    await self._journal_prompt(user, bot)

    async def _kill_time(self, user: User) -> None:
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        await obsidian.safe_generate_today_note()
        # bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        # async with bot.user_interaction():
        # await self._poll_for_metrics(user, bot)
        # await self._journal_prompt(user, bot)

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

        prompt = random.choice(prompts)
        prompt_answer: str = await bot.request_user_input(prompt)
        note_content: str = f"\n\n### {prompt}\n{prompt_answer}\n"
        webhook_api: ObsidianWebhookApi = await ObsidianWebhookApi.for_user(
            user
        )

        date_str: str = datetime.datetime.today().strftime("%Y-%m-%d")
        note_name: str = f"daily/{date_str}.md"
        self._logger.info(f"Appending to note {note_name}")
        await webhook_api.append_to_note(note_name, note_content)
        await bot.send_message("Noted!")

    async def _poll_for_metrics(self, user: User, bot: TelegramBotApi) -> None:
        event_storage: EventStorageRedis = await EventStorageRedis.for_user(
            user
        )

        if (
            event_storage.find_event(
                ActivityType.COLLECT_METRICS, datetime.date.today()
            )
        ) is not None:
            return

        note_content: str = "### Metrics"

        webhook_api: ObsidianWebhookApi = await ObsidianWebhookApi.for_user(
            user
        )

        mood: int = await bot.request_user_choice(
            "How would you rate your mood right now?\n"
            " (1 - negative, 5 - positive)",
            ["1", "2", "3", "4", "5"],
        )
        note_content += "\n" + f"mood:: {mood}"

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
                possible_feelings + ["Cancel"],
            )
            if choice == len(possible_feelings):
                break
            feelings.append(possible_feelings[choice])

        feelings = [f'"{f}"' for f in feelings]
        note_content += "\n" + f"feelings:: [{', '.join(feelings)}]"

        date_str: str = datetime.datetime.today().strftime("%Y-%m-%d")
        note_name: str = f"daily/{date_str}.md"
        self._logger.info(f"Appending to note {note_name}")
        await webhook_api.append_to_note(note_name, note_content)
        await event_storage.add(
            Event(
                date=datetime.date.today(),
                activity_type=ActivityType.COLLECT_METRICS,
                event_type=EventType.DONE,
            )
        )
        await bot.send_message("Noted!")
