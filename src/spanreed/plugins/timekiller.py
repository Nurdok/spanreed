import asyncio
import random
import textwrap
import datetime

from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.apis.obsidian_webhook import ObsidianWebhookApi
from spanreed.user import User
from spanreed.plugin import Plugin
from spanreed.apis.telegram_bot import TelegramBotPlugin


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

    async def _kill_time(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        async with bot.user_interaction():
            await self._journal_prompt(user, bot)

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
