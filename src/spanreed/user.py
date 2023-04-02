import redis
import json


class User:
    redis_api: redis.Redis = None

    def __init__(self):
        self.id: int = -1
        self.config: dict = {}

    def __str__(self):
        return f"User(id={self.id}, name={self.name})"

    @classmethod
    async def create(cls, *, name: str, email: str) -> "User":
        self = User()
        self.id = await cls._generate_user_id()
        self.name = name
        return self

    @classmethod
    async def _generate_user_id(cls) -> int:
        return await cls.redis_api.incr("user:id:counter")

    @classmethod
    async def create_from_db(cls, *, id: int, redis_api: redis.Redis):
        self = User()
        self.id = id
        self.name = await redis_api.get(f"user:{id}:name")
        self.config = json.loads(await redis_api.get(f"user:{id}:config"))
        return self
