import asyncio

import datetime

from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.user import User
from spanreed.plugin import Plugin
from spanreed.apis.telegram_bot import TelegramBotPlugin


class SpanreedMonitorPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Spanreed Monitor"

    async def run_for_user(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        await bot.send_message("Spanreed is starting up.")

        interval: datetime.timedelta = datetime.timedelta(days=1, hours=6)

        try:
            while True:
                await asyncio.sleep(interval.total_seconds())
                await bot.send_message("Spanreed is still running.")
        except asyncio.CancelledError:
            self._logger.info("Spanreed Monitor cancelled.")
            await bot.send_message("Spanreed is shutting down.")
