import asyncio
import traceback
import logging

import datetime
from contextlib import suppress, asynccontextmanager

from typing import AsyncGenerator

import telegram.error

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
                        with suppress(telegram.error.BadRequest):
                            await bot.send_message(
                                f"Exception retrieved from storage:\n\n"
                                f"```python\n{str(exception.decode('utf-8'))}\n```",
                                parse_html=False,
                                parse_markdown=True,
                            )
                        await bot.send_message("Spanreed is still running.")
        except asyncio.CancelledError:
            self._logger.info("Spanreed Monitor cancelled.")
            await bot.send_message("Spanreed is shutting down.")


@asynccontextmanager
async def suppress_and_log_exception(
    *exceptions: type[BaseException],
) -> AsyncGenerator[None, None]:
    logger = logging.getLogger(__name__)
    try:
        yield
    except Exception as e:
        if not any(isinstance(e, exception) for exception in exceptions):
            raise
        exception_str = "".join(
            traceback.format_exception(type(e), e, None, limit=1)
        )
        logger.error(f"Suppressed exception: {exception_str}")
        await redis_api.lpush(
            SpanreedMonitorPlugin.EXCEPTION_QUEUE_NAME, exception_str
        )
