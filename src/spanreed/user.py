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
        # Date of birth. None until the user sets it.
        self.birth_date: datetime.date | None = None

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

    @staticmethod
    def parse_birth_date(text: str) -> datetime.date:
        """Return the date for a date of birth written as ``YYYY-MM-DD``.

        Raise ``ValueError`` if the text is not such a date, or if the date
        is in the future or more than 130 years ago.
        """
        try:
            birth_date = datetime.date.fromisoformat(text.strip())
        except ValueError as e:
            raise ValueError(f"Not a date in the form YYYY-MM-DD: {text!r}") from e
        today = datetime.date.today()
        if birth_date > today or today.year - birth_date.year > 130:
            raise ValueError(f"Not a plausible date of birth: {text!r}")
        return birth_date

    def age_on(self, date: datetime.date) -> int | None:
        """Return the user's age in whole years on ``date``.

        Return ``None`` if the date of birth is not set, or is after ``date``.
        """
        if self.birth_date is None or date < self.birth_date:
            return None
        had_birthday = (date.month, date.day) >= (
            self.birth_date.month,
            self.birth_date.day,
        )
        return date.year - self.birth_date.year - (0 if had_birthday else 1)

    async def set_birth_date(self, text: str) -> None:
        """Validate and store the user's date of birth, written as ``YYYY-MM-DD``."""
        self.birth_date = self.parse_birth_date(text)
        await redis_api.set(f"user:{self.id}:birth_date", self.birth_date.isoformat())

    @classmethod
    async def get_user_birth_date(cls, user_id: int) -> datetime.date | None:
        """Return the stored date of birth of a user, or ``None`` if not set."""
        raw = await redis_api.get(f"user:{user_id}:birth_date")
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            return None

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
        self.birth_date = await cls.get_user_birth_date(user_id)
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
