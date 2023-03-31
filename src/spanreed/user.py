import redis
import json
from typing import Set

class User:
    redis_api: redis.Redis = None

    def __init__(self):
        self.id: int = -1
        self.config: dict = {}

    @classmethod
    async def create(cls, *, name: str, email: str) -> 'User':
        self = User()
        self.id = await cls._generate_user_id()
        self.name = name
        self.email = email
        return self

    @classmethod
    async def _generate_user_id(cls) -> int:
        return await cls.redis_api.incr('user:id:counter')

    @classmethod
    async def create_from_db(cls, *, id: int, redis_api: redis.Redis):
        self = User()
        self.id = id
        self.name = await redis_api.get(f'user:{id}:name')
        self.email = await redis_api.get(f'user:{id}:email')
        self.config = json.loads(await redis_api.get(f'user:{id}:config'))
        return self
