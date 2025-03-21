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
    async def __aenter__(
        self, *args: Any, **kwargs: Any
    ) -> "AsyncContextManager":
        return self

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        pass


def patch_telegram_bot(
    target_package: str,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def patch_telegram_bot_in_target_package(
        f: Callable[..., None],
    ) -> Callable[..., None]:
        def f_with_patched_telegram_bot(*args: list, **kwargs: dict) -> Any:
            with patch(
                f"{target_package}.TelegramBotApi", autospec=True
            ) as mock_bot:
                mock_bot.for_user = AsyncMock(return_value=mock_bot)

                mock_bot.user_interaction = MagicMock(AsyncContextManager())
                mock_bot.request_user_choice = AsyncMock()
                mock_bot.request_user_input = AsyncMock()
                mock_bot.send_message = AsyncMock()
                mock_bot.send_multiple_messages = AsyncMock()

                args = (mock_bot,) + args
                return f(*args, **kwargs)

        return f_with_patched_telegram_bot

    return patch_telegram_bot_in_target_package


def patch_obsidian(
    target_package: str,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def patch_obsidian_in_target_package(
        f: Callable[..., None],
    ) -> Callable[..., None]:
        def f_with_patched_obsidian(*args: list, **kwargs: dict) -> Any:
            with patch(
                f"{target_package}.ObsidianApi", autospec=True
            ) as mock_obsidian:
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


async def async_return_false(*_args: Any, **_kwargs: Any) -> bool:
    logging.getLogger(__name__).info("Returning False")
    return False


async def async_return_true(*_args: Any, **_kwargs: Any) -> bool:
    logging.getLogger(__name__).info("Returning True")
    return True
