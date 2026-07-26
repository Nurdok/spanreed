import builtins
import logging
from typing import Callable, Any, Coroutine
from unittest.mock import MagicMock, patch, AsyncMock
from spanreed.user import User


def patch_redis(f: Callable[..., None]) -> Callable[..., None]:
    def f_with_patched_redis(*args: list, **kwargs: dict) -> None:
        with patch("spanreed.plugin.redis_api", new=MagicMock()) as mock_redis:
            redis_async_defs = [
                "get",
                "set",
                "smembers",
                "sadd",
                "srem",
                "incr",
            ]
            for def_name in redis_async_defs:
                setattr(mock_redis, def_name, AsyncMock())

            args = (mock_redis,) + args
            return f(*args, **kwargs)

    return f_with_patched_redis


def mock_user_find_by_id(user_id: int) -> MagicMock:
    mock_user = MagicMock(name=f"user-{user_id}", spec=User)
    mock_user.id = user_id
    mock_user.name = "Test User"
    mock_user.plugins = []
    return mock_user


class EndPluginRun(Exception):
    pass


class AsyncContextManager:
    async def __aenter__(self, *args: Any, **kwargs: Any) -> "AsyncContextManager":
        return self

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        pass


def patch_user_interaction(
    target_package: str,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Patch the module's ``UserInteractionApi`` with a mock facade.

    Successor of the old ``patch_telegram_bot`` helper: same shape, but
    plugins now talk to the surface-agnostic interaction facade.
    """

    def patch_user_interaction_in_target_package(
        f: Callable[..., None],
    ) -> Callable[..., None]:
        def f_with_patched_user_interaction(*args: list, **kwargs: dict) -> Any:
            with patch(
                f"{target_package}.UserInteractionApi", autospec=True
            ) as mock_bot:
                mock_bot.for_user = AsyncMock(return_value=mock_bot)

                mock_bot.user_interaction = MagicMock(AsyncContextManager())
                mock_bot.request_user_choice = AsyncMock()
                mock_bot.request_user_input = AsyncMock()
                mock_bot.send_message = AsyncMock()
                mock_bot.send_multiple_messages = AsyncMock()
                mock_bot.send_document = AsyncMock()
                mock_bot.edit_message_text = AsyncMock()
                mock_bot.delete_message = AsyncMock()
                mock_bot.notify = AsyncMock()
                mock_bot.register_command = AsyncMock()
                mock_bot.has_active_user_interaction = AsyncMock(return_value=False)

                args = (mock_bot,) + args
                return f(*args, **kwargs)

        return f_with_patched_user_interaction

    return patch_user_interaction_in_target_package


def patch_obsidian(
    target_package: str,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def patch_obsidian_in_target_package(
        f: Callable[..., None],
    ) -> Callable[..., None]:
        def f_with_patched_obsidian(*args: list, **kwargs: dict) -> Any:
            with patch(f"{target_package}.ObsidianApi", autospec=True) as mock_obsidian:
                mock_obsidian.for_user = AsyncMock(return_value=mock_obsidian)

                mock_obsidian.safe_generate_today_note = AsyncMock()
                mock_obsidian.add_value_to_list_property = AsyncMock()
                mock_obsidian.remove_value_from_list_property = AsyncMock()
                mock_obsidian.set_value_of_property = AsyncMock()
                mock_obsidian.delete_property = AsyncMock()
                mock_obsidian.get_property = AsyncMock()
                mock_obsidian.get_daily_note = AsyncMock()
                mock_obsidian.query_dataview = AsyncMock()
                mock_obsidian.list_dir = AsyncMock()
                mock_obsidian.read_file = AsyncMock()
                mock_obsidian.read_binary_file = AsyncMock()
                mock_obsidian.move_file = AsyncMock()

                args = (mock_obsidian,) + args
                return f(*args, **kwargs)

        return f_with_patched_obsidian

    return patch_obsidian_in_target_package


class FakeRedis:
    """In-memory stand-in for the Redis list ops the outbound queues use.

    Lists are modeled head-first (index 0 == left/head), matching Redis:
    LPUSH inserts at the head, so the tail is the oldest element.
    """

    def __init__(self) -> None:
        self.lists: dict[str, list] = {}
        self.strings: dict[str, Any] = {}
        self.hashes: dict[str, dict] = {}
        self.sets: dict[str, set] = {}

    async def lpush(self, key: str, *values: Any) -> int:
        lst = self.lists.setdefault(key, [])
        for value in values:
            lst.insert(0, value)
        return len(lst)

    async def rpush(self, key: str, *values: Any) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    async def lpop(self, key: str) -> Any | None:
        lst = self.lists.get(key, [])
        return lst.pop(0) if lst else None

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        lst = self.lists.get(key, [])
        # end == -1 means "through the tail" (Redis semantics); Python's
        # lst[start:0] would wrongly produce [].
        if end == -1:
            self.lists[key] = lst[start:] if start != 0 else lst
        else:
            self.lists[key] = lst[start : end + 1]
        return True

    async def rpoplpush(self, src: str, dst: str) -> Any | None:
        lst = self.lists.get(src, [])
        if not lst:
            return None
        value = lst.pop()  # tail == oldest
        self.lists.setdefault(dst, []).insert(0, value)
        return value

    async def brpoplpush(self, src: str, dst: str, timeout: int = 0) -> Any | None:
        return await self.rpoplpush(src, dst)

    async def lrem(self, key: str, count: int, value: Any) -> int:
        lst = self.lists.get(key, [])
        removed = 0
        while value in lst and (count == 0 or removed < count):
            lst.remove(value)
            removed += 1
        return removed

    # -- string/hash/set ops used by the Obsidian surface protocol --------

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.strings[key] = value
        return True

    async def get(self, key: str) -> Any | None:
        return self.strings.get(key)

    async def exists(self, *keys: str) -> int:
        return sum(1 for key in keys if key in self.strings)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self.strings, self.hashes, self.sets, self.lists):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    async def incr(self, key: str) -> int:
        value = int(self.strings.get(key, 0)) + 1
        self.strings[key] = value
        return value

    async def hset(self, key: str, field: str, value: Any) -> int:
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hget(self, key: str, field: str) -> Any | None:
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        stored = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in stored:
                del stored[field]
                removed += 1
        return removed

    async def sadd(self, key: str, *members: Any) -> int:
        target = self.sets.setdefault(key, set())
        added = len({str(m) for m in members} - target)
        target.update(str(m) for m in members)
        return added

    async def smembers(self, key: str) -> "builtins.set":
        return builtins.set(self.sets.get(key, builtins.set()))

    async def srem(self, key: str, *members: Any) -> int:
        target = self.sets.setdefault(key, set())
        removed = len(target & {str(m) for m in members})
        target -= {str(m) for m in members}
        return removed

    async def lrange(self, key: str, start: int, end: int) -> list:
        lst = self.lists.get(key, [])
        if end == -1:
            return list(lst[start:])
        return list(lst[start : end + 1])

    async def blpop(
        self, keys: list[str] | str, timeout: int = 0
    ) -> tuple[str, Any] | None:
        if isinstance(keys, str):
            keys = [keys]
        for key in keys:
            lst = self.lists.get(key, [])
            if lst:
                return (key, lst.pop(0))
        return None


async def async_return_false(*_args: Any, **_kwargs: Any) -> bool:
    logging.getLogger(__name__).info("Returning False")
    return False


async def async_return_true(*_args: Any, **_kwargs: Any) -> bool:
    logging.getLogger(__name__).info("Returning True")
    return True
