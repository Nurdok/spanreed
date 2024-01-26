import asyncio

import datetime
from contextlib import suppress

from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.user import User
from spanreed.plugin import Plugin
from spanreed.storage import redis_api


class SpanreedMonitorPlugin(Plugin):
    EXCEPTION_QUEUE_NAME = "spanreed-monitor-exceptions"

    @classmethod
    def name(cls) -> str:
        return "Spanreed Monitor"

    async def run_for_user(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        await bot.send_message("Spanreed is starting up.")

        interval: datetime.timedelta = datetime.timedelta(days=1, hours=6)

        try:
            while True:
                with suppress(asyncio.TimeoutError):
                    async with asyncio.timeout(interval.total_seconds()):
                        _, exception = await redis_api.blpop(
                            self.EXCEPTION_QUEUE_NAME
                        )
                        await bot.send_message(
                            f"Exception retrieved from stroage:\n\n{str(exception.decode('utf-8'))}"
                        )
                        await bot.send_message("Spanreed is still running.")
        except asyncio.CancelledError:
            self._logger.info("Spanreed Monitor cancelled.")
            await bot.send_message("Spanreed is shutting down.")
