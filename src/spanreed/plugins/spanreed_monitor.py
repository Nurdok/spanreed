import asyncio
import traceback
import logging
import json

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
    OBSIDIAN_PLUGIN_MONITOR_QUEUE_NAME = "obsidian-plugin-monitor"

    @classmethod
    def name(cls) -> str:
        return "Spanreed Monitor"

    async def run_for_user(self, user: User) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._monitor_exceptions(user))
            group.create_task(self._monitor_obsidian_plugin(user))

    async def _monitor_exceptions(self, user: User) -> None:
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

    async def _monitor_obsidian_plugin(self, user: User) -> None:
        from spanreed.apis.obsidian import ObsidianPlugin

        obsidian_plugin = await Plugin.get_plugin_by_class(ObsidianPlugin)
        user_ids = (u.id for u in await obsidian_plugin.get_users())
        if user.id not in user_ids:
            self._logger.info(
                "Obsidian plugin not enabled for user, skipping monitoring. "
                f"User: {user.id}, users: {user_ids}"
            )

            return

        queue_name = f"{self.OBSIDIAN_PLUGIN_MONITOR_QUEUE_NAME}:{user.id}"
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        base_timeout = datetime.timedelta(minutes=1)
        last_watchdog = datetime.datetime.now()

        while True:
            self._logger.info("Waiting for Obsidian plugin events.")
            timeout: datetime.timedelta = base_timeout
            time_since_last_watchdog = datetime.datetime.now() - last_watchdog
            if time_since_last_watchdog > base_timeout:
                await bot.send_message("Obsidian plugin watchdog timeout.")
            else:
                timeout -= time_since_last_watchdog

            with suppress(asyncio.TimeoutError):
                async with asyncio.timeout(timeout.total_seconds()):
                    _, event_json = await redis_api.blpop(
                        queue_name,
                    )
                    event = json.loads(event_json)
                    if event["kind"] == "error":
                        self._logger.info(f"Obsidian plugin error: {event}")
                        await redis_api.lpush(
                            self.EXCEPTION_QUEUE_NAME,
                            f"Obsidian plugin error: {event}",
                        )
                    elif event["kind"] == "watchdog":
                        self._logger.info(
                            "Obsidian plugin watchdog event received."
                        )
                        last_watchdog = datetime.datetime.now()


class BoolValue:
    def __init__(self) -> None:
        self.value: bool | None = None

    def set(self, value: bool) -> None:
        if self.value is not None:
            raise RuntimeError("Value already set.")
        self.value = value

    def __bool__(self) -> bool:
        if self.value is None:
            raise RuntimeError("Value not set.")
        return self.value


@asynccontextmanager
async def suppress_and_log_exception(
    *exceptions: type[BaseException],
) -> AsyncGenerator[BoolValue, None]:
    logger = logging.getLogger(__name__)
    did_suppress = BoolValue()
    try:
        yield did_suppress
    except Exception as e:
        if not any(isinstance(e, exception) for exception in exceptions):
            did_suppress.set(False)
            raise
        did_suppress.set(True)
        exception_str = "".join(
            traceback.format_exception(type(e), e, None, limit=3)
        )
        logger.exception(f"Suppressed exception")
        await redis_api.lpush(
            SpanreedMonitorPlugin.EXCEPTION_QUEUE_NAME, exception_str
        )
    else:
        did_suppress.set(False)
