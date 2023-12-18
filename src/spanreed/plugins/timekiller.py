import asyncio

import datetime

from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
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
            pass
