import redis.asyncio as redis
import asyncio
import logging
import json
from typing import Optional, List
from spanreed.user import User
from spanreed.registrable import Registrable
import abc


class Plugin(Registrable):
    plugins: List["Plugin"] = []

    def __init__(self, redis_api: redis.Redis):
        Plugin.register(self)
        self._logger = logging.getLogger(self.name)
        self._redis: redis.Redis = redis_api

    @property
    @abc.abstractmethod
    def name(self) -> str:
        pass

    @property
    def canonical_name(self):
        return self.name.replace(" ", "-").lower()

    def _get_config_key(self, user):
        return f"config:plugin:name={self.canonical_name}:user_id={user.id}"

    def _get_user_list_key(self):
        return f"config:plugin:name={self.canonical_name}:users"

    async def get_config(self, user) -> Optional[dict]:
        config = await self._redis.get(self.get_config_key(user))
        if config is None:
            return None
        return json.loads(config)

    async def get_users(self) -> List[User]:
        self._logger.info(f"Getting users for plugin {self.canonical_name}")
        self._logger.info(f"Getting user list key {self._get_user_list_key()}")
        user_ids = [
            int(user_id)
            for user_id in await self._redis.smembers(
                self._get_user_list_key()
            )
        ]
        users: List[User] = []
        for user_id in user_ids:
            users.append(await User.find_by_id(user_id=user_id))
        self._logger.info(f"Done. Found {len(users)} users.")
        return users

    async def register_user(self, user: User):
        if self.has_user_config():
            from spanreed.apis.telegram_bot import TelegramBotApi

            bot: TelegramBotApi = await TelegramBotApi.for_user(user)
            await bot.send_multiple_messages(
                f"The {self.name} plugin requires some configuration.",
                "Let's do that now.",
            )
            await self.ask_for_user_config(user)

        await self._redis.sadd(f"user:{user.id}:plugins", self.canonical_name)
        await self._redis.sadd(self._get_user_list_key(), user.id)

    async def unregister_user(self, user: User):
        await self._redis.srem(f"user:{user.id}:plugins", self.canonical_name)
        await self._redis.srem(self._get_user_list_key(), user.id)

    async def run_for_user(self, user: User):
        pass

    # This currently assumes that user <--> plugin subscription doesn't
    # change during the lifetime of the plugin.
    # TODO: Refactor to allow for dynamic subscription changes.
    async def run(self):
        self._logger.info(f"Running plugin {self.canonical_name}")
        try:
            coros = []
            for user in await self.get_users():
                self._logger.info(
                    f"Running plugin {self.canonical_name} for user {user.id}"
                )
                coros.append(self.run_for_user(user))

            await asyncio.gather(*coros)
        except:
            self._logger.exception("Exception in plugin run")

    @classmethod
    def register(cls, plugin):
        if plugin.canonical_name in [p.canonical_name for p in cls.plugins]:
            raise ValueError(
                f"Plugin with name {plugin.canonical_name} already registered."
            )
        cls.plugins.append(plugin)

    @classmethod
    async def get_all_plugins(cls):
        return cls.plugins

    @classmethod
    async def get_plugin_by_class(cls, plugin_cls):
        for plugin in cls.plugins:
            logging.getLogger(__name__).info(
                f"Checking {plugin.__class__=} against {plugin_cls=}"
            )
            if plugin.__class__ == plugin_cls:
                return plugin
        return None

    @classmethod
    async def get_plugins_for_user(cls, user: User):
        plugins = []
        for plugin in cls.plugins:
            if await plugin.is_subscribed(user):
                plugins.append(plugin)
        return plugins

    async def is_subscribed(self, user: User) -> bool:
        return await self._redis.sismember(
            f"user:{user.id}:plugins", self.canonical_name
        )
