import redis.asyncio as redis
from typing import Optional, cast
from spanreed.storage import redis_api


class User:
    def __init__(self) -> None:
        self.id: int = -1
        self.plugins: list[str] = []
        self.name: str = ""

    def __str__(self) -> str:
        return f"{self.name} (id {self.id})"

    def __repr__(self) -> str:
        return f"User(id={self.id}, name={self.name})"

    async def set_name(self, name: str) -> None:
        self.name = name
        await redis_api.set(f"user:{self.id}:name", name)

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
        return cast(bytes, await redis_api.get(f"user:{user_id}:name")).decode(
            "utf-8"
        )

    @classmethod
    async def find_by_id(cls, user_id: int) -> "User":
        self = User()
        self.id = user_id
        self.name = await cls.get_user_name(user_id)
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
