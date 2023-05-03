from typing import Callable, Any
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
    async def __aenter__(self, *args, **kwargs):
        return self

    async def __aexit__(self, *args, **kwargs):
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
