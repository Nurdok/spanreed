from typing import Callable, Any
from unittest.mock import MagicMock, patch, AsyncMock
from spanreed.user import User


def patch_redis(f: Callable) -> Callable[..., MagicMock]:
    def f_with_patched_redis(*args: list, **kwargs: dict) -> Any:
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
            return f(*args, **kwargs, mock_redis=mock_redis)

    return f_with_patched_redis


def mock_user_find_by_id(user_id: int) -> MagicMock:
    mock_user = MagicMock(name=f"user-{user_id}", spec=User)
    mock_user.id = user_id
    mock_user.name = "Test User"
    mock_user.plugins = []
    return mock_user
