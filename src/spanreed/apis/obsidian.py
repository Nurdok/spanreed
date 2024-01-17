import logging
import uuid
import json
import asyncio
import datetime

import aiohttp

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.storage import redis_api
from spanreed.apis.telegram_bot import TelegramBotApi

from typing import Any


class ObsidianPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Obsidian"


class ObsidianApi:
    def __init__(self, user: User) -> None:
        self._logger = logging.getLogger(__name__)
        self._user = user

    @classmethod
    async def for_user(cls, user: User) -> "ObsidianApi":
        return ObsidianApi(user)

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        request_id: int = uuid.uuid4().int
        request: dict[str, Any] = {
            # Needs to be a string because the TS JSON parser can't handle ints that big
            "request_id": str(request_id),
            "method": method,
            "params": params or {},
        }
        await redis_api.lpush(
            f"obsidian-plugin-tasks:{self._user.id}",
            json.dumps(request),
        )

        try:
            async with asyncio.timeout(datetime.timedelta(seconds=10).seconds):
                queue_name: str = (
                    f"obsidian-plugin-tasks:{self._user.id}:{request_id}"
                )
                self._logger.info(f"Waiting for response on {queue_name=}")
                response: dict = json.loads(
                    # Take the value from the tuple returned by `blpop`, we already know the key
                    (await redis_api.blpop(queue_name))[1]
                )
        except TimeoutError:
            response = {
                "success": False,
                "error": "TimeoutError",
                "error_message": "The request timed out.",
            }

        if not response["success"]:
            msg: str = (
                f"Obsidian API request failed:\n\t{request=}\n\t{response=}"
            )
            bot: TelegramBotApi = await TelegramBotApi.for_user(self._user)
            await bot.send_message(msg)
            raise RuntimeError(msg)

        return response["result"]

    async def safe_generate_today_note(self) -> None:
        await self._send_request("generate-daily-note")
