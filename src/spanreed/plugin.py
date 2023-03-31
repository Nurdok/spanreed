import redis
import asyncio
import logging
import json
from typing import Optional, List
from spanreed.user import User
import spanreed
import abc


class Plugin(abc.ABC):
    def __init__(self, redis_api: redis.Redis):
        self._logger = logging.getLogger(self.name)
        self._logger.info(f"Plugin {self.canonical_name} initialized")
        self._redis: redis.Redis = redis_api

    @property
    @abc.abstractmethod
    def name(self) -> str:
        pass

    @property
    def canonical_name(self):
        return self.name.replace(' ', '-').lower()

    def _get_config_key(self, user):
        return f'config:plugin:name={self.canonical_name}:user_id={user.id}'

    def _get_user_list_key(self):
        return f'config:plugin:name={self.canonical_name}:users'

    async def get_config(self, user) -> Optional[dict]:
        config = await self._redis.get(self.get_config_key(user))
        if config is None:
            return None
        return json.loads(config)

    async def get_users(self) -> List[User]:
        user_ids = [int(user_id) for user_id
                    in await self._redis.smembers(self._get_user_list_key())]
        users: List[User] = []
        for user_id in user_ids:
            users.append(await User.create_from_db(id=user_id,
                                                   redis_api=self._redis))
        return users

    async def run_for_user(self, user):
        pass

    # This currently assumes that user <--> plugin subscription doesn't
    # change during the lifetime of the plugin.
    # Refactor to allow for dynamic subscription changes.
    async def run(self):
        coros = []
        for user in await self.get_users():
            self._logger.info(f"Running plugin {self.canonical_name} for user {user.id}")
            coros.append(self.run_for_user(user))

        await asyncio.gather(*coros)

