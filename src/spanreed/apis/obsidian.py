import logging
import typing
import uuid
import json
import asyncio
import datetime
import dataclasses
import base64

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.storage import redis_api
from spanreed.apis.telegram_bot import TelegramBotApi

from typing import Any


class ObsidianApiTimeoutError(TimeoutError):
    pass


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

    async def _run_progress_bar(
        self,
        attempt: int,
        max_attempts: int,
        timeout_seconds: float,
    ) -> None:
        """Background task that shows a Telegram progress bar after 5s delay.

        Sets self._progress_message when the first message is sent.
        """
        await asyncio.sleep(5)
        bot: TelegramBotApi = await TelegramBotApi.for_user(self._user)
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = (
                asyncio.get_event_loop().time() - start_time + 5
            )  # include initial delay
            fraction = min(elapsed / timeout_seconds, 1.0)
            filled = int(fraction * 10)
            bar = "█" * filled + "░" * (10 - filled)
            text = f"⏳ Waiting for Obsidian [{bar}] (attempt {attempt}/{max_attempts})"
            if self._progress_message is None:
                self._progress_message = await bot.send_message(text, parse_html=False)
            else:
                try:
                    await self._progress_message.edit_text(text)
                except Exception:
                    pass  # telegram raises if text hasn't changed
            await asyncio.sleep(3)

    @staticmethod
    def _summarize_request(request: dict[str, Any]) -> dict[str, Any]:
        """Copy of a request with large binary payloads elided, for logging."""
        import copy

        safe = copy.deepcopy(request)
        params = safe.get("params")
        if isinstance(params, dict) and isinstance(params.get("content"), str):
            content = params["content"]
            if len(content) > 64:
                params["content"] = f"<{len(content)} base64 chars elided>"
        return safe

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        max_attempts = 3
        request_queue_name: str = f"obsidian-plugin-tasks:{self._user.id}"
        self._progress_message = None

        for attempt in range(1, max_attempts + 1):
            request_id: int = uuid.uuid4().int
            request: dict[str, Any] = {
                # Needs to be a string because the TS JSON parser can't handle ints that big
                "request_id": str(request_id),
                "method": method,
                "params": params or {},
            }
            self._logger.info(
                f"Sending request: {request=} (attempt {attempt}/{max_attempts})"
            )

            try:
                async with asyncio.timeout(
                    datetime.timedelta(seconds=30).total_seconds()
                ):
                    await redis_api.lpush(request_queue_name, json.dumps(request))
            except Exception:
                self._logger.error(f"Failed to send request: {request=}")
                raise

            progress_task = asyncio.create_task(
                self._run_progress_bar(attempt, max_attempts, 30)
            )

            try:
                async with asyncio.timeout(
                    datetime.timedelta(seconds=30).total_seconds()
                ):
                    response_queue_name: str = (
                        f"obsidian-plugin-tasks:{self._user.id}:{request_id}"
                    )
                    self._logger.info(f"Waiting for response on {response_queue_name=}")
                    response: dict = json.loads(
                        # Take the value from the tuple returned by `blpop`, we already know the key
                        (await redis_api.blpop(response_queue_name))[1]
                    )
                    self._logger.info(
                        f"Got response for {method=} on {response_queue_name=}: {response=}"
                    )
            except TimeoutError:
                progress_task.cancel()
                # Delete the request from the queue
                await redis_api.lrem(request_queue_name, 0, json.dumps(request))
                if attempt < max_attempts:
                    self._logger.warning(
                        f"Obsidian API request timed out ({request_id=}), "
                        f"retrying (attempt {attempt}/{max_attempts})"
                    )
                    continue
                # Final attempt failed
                if self._progress_message is not None:
                    try:
                        await self._progress_message.edit_text(
                            "❌ Obsidian didn't respond"
                        )
                    except Exception:
                        pass
                raise ObsidianApiTimeoutError(
                    f"Obsidian API request timed out after {max_attempts} attempts ({request_id=})."
                )
            else:
                progress_task.cancel()
                break

        # Clean up progress message on success
        if self._progress_message is not None:
            try:
                await self._progress_message.delete()
            except Exception:
                pass

        self._logger.info(f"Got response: {response=}")

        if not response["success"]:
            # Elide any large base64 payload (e.g. a write-file's `content`)
            # before building the debug message. Otherwise the message can
            # exceed Telegram's 4096-char limit, and the resulting "Text is too
            # long" send error masks the real failure reason.
            safe_request = self._summarize_request(request)
            msg: str = (
                f"Obsidian API request failed:\n\t{safe_request!r}\n\t{response!r}"
            )
            if len(msg) > 1500:
                msg = msg[:1500] + " …(truncated)"
            bot: TelegramBotApi = await TelegramBotApi.for_user(self._user)
            await bot.send_message(msg)
            result: Any = response.get("result")
            if result == "file not found":
                raise FileNotFoundError(str(result))
            if isinstance(result, str) and "Destination file already exists" in result:
                raise FileExistsError(str(result))
            raise RuntimeError(f"Obsidian request '{method}' failed: {result}")

        return response.get("result", None)

    async def safe_generate_today_note(self) -> None:
        await self._send_request("generate-daily-note")

    async def add_value_to_list_property(
        self, filepath: str, property_name: str, value: str
    ) -> None:
        await self._send_request(
            "modify-property",
            {
                "filepath": filepath,
                "operation": "addToList",
                "property": property_name,
                "value": value,
            },
        )

    async def remove_value_from_list_property(
        self, filepath: str, property_name: str, value: str
    ) -> None:
        await self._send_request(
            "modify-property",
            {
                "filepath": filepath,
                "operation": "removeFromList",
                "property": property_name,
                "value": value,
            },
        )

    async def set_value_of_property(
        self, filepath: str, property_name: str, value: str | list[str]
    ) -> None:
        await self._send_request(
            "modify-property",
            {
                "filepath": filepath,
                "operation": "setSingleValue",
                "property": property_name,
                "value": value,
            },
        )

    async def delete_property(self, filepath: str, property_name: str) -> None:
        await self._send_request(
            "modify-property",
            {
                "filepath": filepath,
                "operation": "deleteProperty",
                "property": property_name,
            },
        )

    async def get_property(self, filepath: str, property_name: str) -> Any:
        property_value: Any = await self._send_request(
            "modify-property",
            {
                "filepath": filepath,
                "operation": "getProperty",
                "property": property_name,
            },
        )
        self._logger.info(f"Got property value: {property_value=}")
        return property_value

    async def get_daily_note(
        self, daily_note_path: str, date: datetime.date | None = None
    ) -> str:
        if date is None:
            date = datetime.date.today()
        # TODO: Query the API for the daily note for a specific date
        filename = f"{date.strftime('%Y-%m-%d')}.md"
        if daily_note_path is None or daily_note_path == "":
            return filename
        return f"{daily_note_path}/{filename}"

    async def query_dataview(self, query: str) -> list[Any] | Any:
        query_result = await self._send_request("query-dataview", {"query": query})
        self._logger.info(f"Got query result: {query_result=}")
        result_type = query_result.get("type", None)
        if result_type == "list":
            return typing.cast(list[Any], query_result["values"])
        if result_type == "table":
            return [
                dataclasses.make_dataclass(
                    "QueryResultRow",
                    [
                        (header_name.lower(), str)
                        for header_name in query_result["headers"]
                    ],
                )(*values)
                for values in query_result["values"]
            ]
        return query_result

    async def list_dir(self, dirpath: str) -> list[str]:
        return typing.cast(
            list[str],
            await self._send_request("list-dir", {"dirpath": dirpath}),
        )

    async def read_file(self, filepath: str) -> str:
        return typing.cast(
            str,
            await self._send_request(
                "read-file", {"filepath": filepath, "format": "text"}
            ),
        )

    async def read_binary_file(self, filepath: str) -> bytes:
        content_base64: str = (
            await self._send_request(
                "read-file", {"filepath": filepath, "format": "binary"}
            )
        )["content"]
        return base64.b64decode(content_base64)

    async def write_binary_file(
        self, filepath: str, data: bytes, *, overwrite: bool = False
    ) -> None:
        """Write a binary file into the vault (symmetric to read_binary_file).

        Sends the content base64-encoded via the ``write-file`` method. The
        companion plugin is expected to create any missing parent folders and,
        when ``overwrite`` is false and the file already exists, fail with a
        "Destination file already exists" message (which surfaces here as
        ``FileExistsError``, matching ``move-file``).
        """
        await self._send_request(
            "write-file",
            {
                "filepath": filepath,
                "format": "binary",
                "content": base64.b64encode(data).decode("ascii"),
                "overwrite": overwrite,
            },
        )

    async def move_file(self, from_path: str, to_path: str) -> None:
        await self._send_request("move-file", {"from": from_path, "to": to_path})

    async def delete_file(self, filepath: str) -> None:
        """Delete a file from the vault (moved to trash by the companion plugin)."""
        await self._send_request("delete-file", {"filepath": filepath})
