import asyncio
import json

import datetime
from contextlib import suppress

import redis

from spanreed import errors
from spanreed.interaction import UserInteractionApi
from spanreed.user import User
from spanreed.plugin import Plugin
from spanreed.storage import redis_api


class SpanreedMonitorPlugin(Plugin):
    EXCEPTION_QUEUE_NAME = errors.QUEUE
    OBSIDIAN_PLUGIN_MONITOR_QUEUE_NAME = "obsidian-plugin-monitor"
    OBSIDIAN_PLUGIN_ERROR_IGNORE_LIST = [
        "read ECONNRESET",
        "read ETIMEDOUT",
        "Socket closed unexpectedly",
    ]

    @classmethod
    def name(cls) -> str:
        return "Spanreed Monitor"

    async def run_for_user(self, user: User) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._monitor_exceptions(user))
            group.create_task(self._monitor_obsidian_plugin(user))

    async def _monitor_exceptions(self, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        # Durable: delivered once the bot can send, even if it's flood-limited
        # at startup.
        await bot.notify("Spanreed is starting up.")

        interval: datetime.timedelta = datetime.timedelta(days=1, hours=6)

        try:
            while True:
                with suppress(asyncio.TimeoutError, redis.ConnectionError):
                    async with asyncio.timeout(interval.total_seconds()):
                        _, exception = await redis_api.blpop(self.EXCEPTION_QUEUE_NAME)
                        await bot.notify(
                            f"Exception retrieved from storage:\n\n"
                            f"```python\n{str(exception.decode('utf-8'))}\n```",
                            parse_html=False,
                            parse_markdown=True,
                        )
                        await bot.notify("Spanreed is still running.")
                        await redis_api.delete(self.EXCEPTION_QUEUE_NAME)
        except asyncio.CancelledError:
            self._logger.info("Spanreed Monitor cancelled.")
            # Best-effort immediate send: the process (and the outbound
            # dispatcher) is going away, so a queued message wouldn't be
            # delivered until the next startup. The timeout covers the case
            # where the dispatcher task was already cancelled and the send
            # would otherwise wait forever.
            with suppress(Exception):
                async with asyncio.timeout(5):
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
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        base_timeout = datetime.timedelta(minutes=1)
        time_since_last_watchdog = datetime.timedelta()
        last_watchdog_message = datetime.datetime.now()
        last_obsidian_error_message = datetime.datetime.now()

        while True:
            self._logger.debug("Waiting for Obsidian plugin events.")
            timeout: datetime.timedelta = base_timeout
            time_since_last_watchdog_message = (
                datetime.datetime.now() - last_watchdog_message
            )
            if (
                time_since_last_watchdog > base_timeout
                and time_since_last_watchdog_message > datetime.timedelta(minutes=30)
            ):
                await bot.send_message("Obsidian plugin watchdog timeout.")
                last_watchdog_message = datetime.datetime.now()
                timeout = base_timeout
            else:
                timeout -= time_since_last_watchdog

            with suppress(asyncio.TimeoutError):
                self._logger.debug(
                    f"Waiting for event on {queue_name} with timeout {timeout}"
                )
                async with asyncio.timeout(timeout.total_seconds()):
                    _, event_json = await redis_api.blpop(
                        queue_name,
                    )
                    event = json.loads(event_json)
                    self._logger.info(f"Obsidian plugin event received: {event}")
                    if event["kind"] == "error":
                        self._logger.info(f"Obsidian plugin error: {event}")
                        if event["message"] in self.OBSIDIAN_PLUGIN_ERROR_IGNORE_LIST:
                            continue
                        time_since_last_obsidian_error_message = (
                            datetime.datetime.now() - last_obsidian_error_message
                        )
                        if time_since_last_obsidian_error_message > datetime.timedelta(
                            minutes=30
                        ):
                            await redis_api.lpush(
                                self.EXCEPTION_QUEUE_NAME,
                                f"Obsidian plugin error: {event}",
                            )
                            last_obsidian_error_message = datetime.datetime.now()
                    elif event["kind"] == "watchdog":
                        self._logger.debug("Obsidian plugin watchdog event received.")
                        time_since_last_watchdog = datetime.timedelta()
                        last_watchdog_message = datetime.datetime.now()
