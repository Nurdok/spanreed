import datetime
import zoneinfo

import redis.asyncio as redis
from typing import Optional, cast
from spanreed.storage import redis_api


class User:
    def __init__(self) -> None:
        self.id: int = -1
        self.plugins: list[str] = []
        self.name: str = ""
        # IANA timezone name, for example "Asia/Jerusalem". None until the
        # user sets it.
        self.timezone: str | None = None

    def __str__(self) -> str:
        return f"{self.name} (id {self.id})"

    def __repr__(self) -> str:
        return f"User(id={self.id}, name={self.name})"

    async def set_name(self, name: str) -> None:
        self.name = name
        await redis_api.set(f"user:{self.id}:name", name)

    @staticmethod
    def parse_timezone(name: str) -> datetime.tzinfo:
        """Return the tzinfo for an IANA timezone name.

        Raise ``ValueError`` if the name is not a known timezone.
        """
        try:
            return zoneinfo.ZoneInfo(name.strip())
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"Unknown timezone: {name!r}") from e

    @property
    def tzinfo(self) -> datetime.tzinfo:
        """Return the user's timezone.

        Fall back to the server's local timezone if the user has not set
        one, so that callers get the same dates as the other plugins that
        use the server clock.
        """
        if self.timezone is None:
            return datetime.datetime.now().astimezone().tzinfo or datetime.timezone.utc
        return self.parse_timezone(self.timezone)

    async def set_timezone(self, name: str) -> None:
        """Validate and store the user's IANA timezone name."""
        self.parse_timezone(name)
        self.timezone = name.strip()
        await redis_api.set(f"user:{self.id}:timezone", self.timezone)

    @classmethod
    async def get_user_timezone(cls, user_id: int) -> str | None:
        raw = await redis_api.get(f"user:{user_id}:timezone")
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def set_plugins(self, plugins: list[str]) -> None:
        self.plugins = plugins
        for plugin in plugins:
            await redis_api.sadd(f"config:plugin:name={plugin}:users", self.id)
            await redis_api.sadd(f"user:{self.id}:plugins", plugin)

    @classmethod
    async def create(cls, name: str = "Master") -> "User":
        self = User()
        self.id = await cls._generate_user_id()
        await self.set_name(name)
        await self.set_plugins([])
        return self

    @classmethod
    async def _generate_user_id(cls) -> int:
        return await redis_api.incr("user:id:counter")

    @classmethod
    async def get_user_name(cls, user_id: int) -> str:
        return cast(bytes, await redis_api.get(f"user:{user_id}:name")).decode("utf-8")

    @classmethod
    async def find_by_id(cls, user_id: int) -> "User":
        self = User()
        self.id = user_id
        self.name = await cls.get_user_name(user_id)
        self.timezone = await cls.get_user_timezone(user_id)
        self.plugins = [
            name.decode("utf-8")
            for name in await redis_api.smembers(f"user:{self.id}:plugins")
        ]
        return self

    @classmethod
    async def get_user_counter(cls) -> int:
        counter: Optional[int] = await redis_api.get("user:id:counter")
        if counter is None:
            raise RuntimeError("User counter not initialized.")
        return int(counter)

    @classmethod
    async def get_all_users(cls) -> list["User"]:
        users = []
        for user_id in range(await cls.get_user_counter() + 1):
            users.append(await cls.find_by_id(user_id=user_id))
        return users
