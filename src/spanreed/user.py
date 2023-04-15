import redis.asyncio as redis
import json
from typing import List


class User:
    redis_api: redis.Redis = None

    def __init__(self):
        self.id: int = -1
        self.config: dict = {}
        self.plugins: List[str] = []
        self.name: str = ""

    def __str__(self):
        return f"{self.name} (id {self.id})"

    async def set_name(self, name: str):
        self.name = name
        await self.redis_api.set(f"user:{self.id}:name", name)

    async def set_config(self, config: dict):
        self.config = config
        await self.redis_api.set(f"user:{self.id}:config", json.dumps(config))

    async def set_plugins(self, plugins: List[str]):
        await self.redis_api.sadd(f"user:{self.id}:plugins", plugins)

    @classmethod
    async def create(cls, name: str):
        self = User()
        self.id = await cls._generate_user_id()
        await self.set_name(name)
        await self.set_config({})
        await self.set_plugins([])
        return self

    @classmethod
    async def _generate_user_id(cls) -> int:
        return await cls.redis_api.incr("user:id:counter")

    @classmethod
    async def find_by_id(cls, user_id: int) -> "User":
        self = User()
        self.id = user_id
        self.name = (await cls.redis_api.get(f"user:{user_id}:name")).decode(
            "utf-8"
        )
        self.config = json.loads(
            await cls.redis_api.get(f"user:{user_id}:config")
        )
        self.plugins = [
            name.decode("utf-8")
            for name in await cls.redis_api.smembers(f"user:{user_id}:plugins")
        ]
        return self

    @classmethod
    async def get_all_users(cls, redis_api: redis.Redis) -> List["User"]:
        users = []
        for user_id in range(int(await redis_api.get("user:id:counter")) + 1):
            users.append(await cls.find_by_id(user_id=user_id))
        return users
