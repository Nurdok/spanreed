from __future__ import annotations

import redis.asyncio as redis
import asyncio
import logging
import json
from typing import Generic, TypeVar
from spanreed.user import User
import abc
from dataclasses import asdict

UC = TypeVar("UC")


class Plugin(abc.ABC, Generic[UC]):
    _plugins: list[Plugin] = []
    _redis: redis.Redis

    def __init__(self, redis_api: redis.Redis):
        Plugin.register(self)
        self._logger = logging.getLogger(self.name())
        self._redis: redis.Redis = redis_api

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        pass

    @classmethod
    def canonical_name(cls):
        return cls.name().replace(" ", "-").lower()

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return []

    @classmethod
    def _get_config_key(cls, user):
        return f"config:plugin:name={cls.canonical_name()}:user_id={user.id}"

    @classmethod
    def _get_user_list_key(cls):
        return f"config:plugin:name={cls.canonical_name()}:users"

    @classmethod
    def has_user_config(cls):
        return False

    @classmethod
    def get_config_class(cls) -> type[UC]:
        return None

    # TODO: fancy typing stuff to get the UserConfig class
    @classmethod
    async def get_config(cls, user: User) -> UC:
        if cls.get_config_class() is None:
            raise NotImplementedError("This plugin does not have user config.")

        config_class = cls.get_config_class()
        config = await user.redis_api.get(cls._get_config_key(user))
        if config is None:
            return config_class()
        return config_class(**json.loads(config))

    async def set_config(self, user: User, config: UC) -> None:
        await self._redis.set(
            self._get_config_key(user), json.dumps(asdict(config))
        )

    async def get_users(self) -> list[User]:
        self._logger.info(f"Getting users for plugin {self.canonical_name()}")
        self._logger.info(f"Getting user list key {self._get_user_list_key()}")
        user_ids = [
            int(user_id)
            for user_id in await self._redis.smembers(
                self._get_user_list_key()
            )
        ]
        users: list[User] = []
        for user_id in user_ids:
            users.append(await User.find_by_id(user_id=user_id))
        self._logger.info(f"Done. Found {len(users)} users.")
        return users

    async def register_user(self, user: User):
        from spanreed.apis.telegram_bot import TelegramBotApi

        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        unregistered_plugins: list[Plugin] = [
            plugin
            for plugin_cls in self.get_prerequisites()
            if not await (
                plugin := await self.get_plugin_by_class(plugin_cls)
            ).is_registered(user)
        ]

        if unregistered_plugins:
            choice: int = await bot.request_user_choice(
                "This plugin requires some other plugins you haven't"
                " configured yet.\nWould you like to do that right now?",
                ["Okay", "Cancel"],
            )
            if choice != 0:
                await bot.send_message("Okay, we'll skip this plugin for now.")
                return

            for plugin in unregistered_plugins:
                await plugin.register_user(user)

        if self.has_user_config():
            await bot.send_multiple_messages(
                f"The {self.name()} plugin requires some configuration.",
                "Let's do that now.",
            )
            await self.ask_for_user_config(user)

        await self._redis.sadd(
            f"user:{user.id}:plugins", self.canonical_name()
        )
        await self._redis.sadd(self._get_user_list_key(), user.id)

    async def unregister_user(self, user: User):
        await self._redis.srem(
            f"user:{user.id}:plugins", self.canonical_name()
        )
        await self._redis.srem(self._get_user_list_key(), user.id)

    async def run_for_user(self, user: User):
        pass

    # This currently assumes that user <--> plugin subscription doesn't
    # change during the lifetime of the plugin.
    # TODO: Refactor to allow for dynamic subscription changes.
    async def run(self):
        self._logger.info(f"Running plugin {self.canonical_name()}")
        try:
            coros = []
            for user in await self.get_users():
                self._logger.info(
                    f"Running plugin {self.canonical_name()} for user {user.id}"
                )
                coros.append(self.run_for_user(user))

            await asyncio.gather(*coros)
        except Exception:
            self._logger.exception("Exception in plugin run")

    @classmethod
    def register(cls, plugin):
        if plugin.canonical_name() in [
            p.canonical_name() for p in cls._plugins
        ]:
            raise ValueError(
                f"Plugin with name {plugin.canonical_name()} already registered."
            )
        cls._plugins.append(plugin)

    @classmethod
    async def get_all_plugins(cls):
        return cls._plugins

    @classmethod
    async def get_plugin_by_class(cls, plugin_cls) -> "Plugin":
        for plugin in cls._plugins:
            logging.getLogger(__name__).info(
                f"Checking {plugin.__class__=} against {plugin_cls=}"
            )
            if plugin.__class__ == plugin_cls:
                return plugin

        raise ValueError(f"Plugin {plugin_cls} not found.")

    @classmethod
    async def get_plugins_for_user(cls, user: User):
        plugins = []
        for plugin in cls._plugins:
            if await plugin.is_registered(user):
                plugins.append(plugin)
        return plugins

    async def is_registered(self, user: User) -> bool:
        return await self._redis.sismember(
            f"user:{user.id}:plugins", self.canonical_name()
        )
