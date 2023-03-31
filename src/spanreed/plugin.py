import redis
import logging
import json
from typing import Optional
import abc


class Plugin(abc.ABC):
    def __init__(self, name, redis_api: redis.Redis):
        self._name = name
        self._logger = logging.getLogger(name)
        self._logger.info(f"Plugin {self.canonical_name} initialized")
        self._redis: redis.Redis = redis_api

    @property
    def canonical_name(self):
        return self._name.replace(' ', '-').lower()

    def _get_config_key(self, user):
        return f'config:plugin:name={self.canonical_name}:user_id={user.id}'

    async def get_config(self, user) -> Optional[dict]:
        config = await self._redis.get(self.get_config_key(user))
        if config is None:
            return None
        return json.loads(config)

    @abc.abstractmethod
    async def run(self, user):
        pass

