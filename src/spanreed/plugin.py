from __future__ import annotations

from spanreed.storage import redis_api
import logging
import json
from typing import TYPE_CHECKING, Generic, TypeVar, Optional, cast
from spanreed.user import User
import abc
from dataclasses import asdict

if TYPE_CHECKING:
    from spanreed.workflows.blocks import Block

UC = TypeVar("UC")


class Plugin(abc.ABC, Generic[UC]):
    BASE_LOGGER = logging.getLogger("spanreed.plugin")
    _plugins: list[Plugin] = []

    def __init__(self) -> None:
        Plugin.register(self)
        self._logger = self.BASE_LOGGER.getChild(self.name())

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        pass

    @classmethod
    def canonical_name(cls) -> str:
        return cls.name().replace(" ", "-").lower()

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return []

    def blocks(self) -> list[Block]:
        """Return the blocks that this plugin gives to workflows.

        A user's workflows can use them while the user is registered to
        the plugin. See ``spanreed.workflows.blocks``.
        """
        return []

    @classmethod
    def _get_config_key(cls, user: User) -> str:
        return f"config:plugin:name={cls.canonical_name()}:user_id={user.id}"

    @classmethod
    def _get_user_list_key(cls) -> str:
        return f"config:plugin:name={cls.canonical_name()}:users"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        if cls.has_user_config():
            raise NotImplementedError("This plugin does not have user config.")

    @classmethod
    def get_config_class(cls) -> Optional[type[UC]]:
        return None

    # TODO: fancy typing stuff to get the UserConfig class
    @classmethod
    async def get_config(cls, user: User) -> UC:
        config_class = cls.get_config_class()
        if config_class is None:
            raise NotImplementedError("This plugin does not have user config.")

        config = await redis_api.get(cls._get_config_key(user))
        if config is None:
            return config_class()
        return config_class(**json.loads(config))

    @classmethod
    async def get_saved_config_data(cls, user: User) -> Optional[dict]:
        """Return the user's saved config as JSON data.

        Return ``None`` if no config is saved for the user.
        """
        config = await redis_api.get(cls._get_config_key(user))
        if config is None:
            return None
        return cast(dict, json.loads(config))

    @classmethod
    async def set_config(cls, user: User, config: UC) -> None:
        if cls.get_config_class() is None or type(config) != cls.get_config_class():
            raise NotImplementedError(
                "This plugin does not have user config, or the passed "
                "config is of the wrong type."
                f"Expected {cls.get_config_class()} (!= None), "
                f"got {type(config)}."
            )

        await redis_api.set(
            cls._get_config_key(user),
            json.dumps(asdict(config)),  # type: ignore
        )

    @classmethod
    def _get_user_data_key(cls, user: User, key: str) -> str:
        return f"plugin-data:plugin:name={cls.canonical_name()}:user_id={user.id}:{key}"

    @classmethod
    async def set_user_data(cls, user: User, key: str, data: str) -> None:
        await redis_api.set(cls._get_user_data_key(user, key), data)

    @classmethod
    async def get_user_data(cls, user: User, key: str) -> Optional[str]:
        data: str | bytes = cast(
            str | bytes, await redis_api.get(cls._get_user_data_key(user, key))
        )
        if isinstance(data, bytes):
            return data.decode("utf-8")
        return data

    async def user_ids(self) -> set[int]:
        """Return the ids of the users who are registered to the plugin."""
        return {
            int(user_id)
            for user_id in await redis_api.smembers(self._get_user_list_key())
        }

    async def get_users(self) -> list[User]:
        self._logger.info(f"Getting users for plugin {self.canonical_name()}")
        self._logger.info(f"Getting user list key {self._get_user_list_key()}")
        user_ids = [
            int(user_id)
            for user_id in await redis_api.smembers(self._get_user_list_key())
        ]
        users: list[User] = []
        for user_id in user_ids:
            users.append(await User.find_by_id(user_id=user_id))
        self._logger.info(f"Done. Found {len(users)} users.")
        return users

    async def register_user(self, user: User) -> None:
        self._logger.info(f"Registering user {user.id} to plugin")
        from spanreed.interaction import UserInteractionApi

        bot: UserInteractionApi = await UserInteractionApi.for_user(user)

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

        await redis_api.sadd(f"user:{user.id}:plugins", self.canonical_name())
        await redis_api.sadd(self._get_user_list_key(), user.id)
        self._notify_user_commands_changed(user)

    async def unregister_user(self, user: User) -> None:
        await redis_api.srem(f"user:{user.id}:plugins", self.canonical_name())
        await redis_api.srem(self._get_user_list_key(), user.id)
        self._notify_user_commands_changed(user)

    def _notify_user_commands_changed(self, user: User) -> None:
        # The user's effective command set changed at runtime; without this
        # trigger a plugin registered via the plugin manager would never
        # reach the (phase 3+) Obsidian palette until the next restart.
        from spanreed.interaction import commands

        commands.notify_user_plugins_changed(user)

    async def run(self) -> None:
        """Run what the plugin has to run for as long as Spanreed does.

        Nothing, by default: what plugins did for their users are workflows
        now. A plugin overrides this to register its commands, to serve a
        surface, or to listen to its service for each of its users (see
        ``spanreed.service.serve_users``).
        """

    @classmethod
    def reset_registry(cls) -> None:
        cls._plugins = []

    @classmethod
    def register(cls, plugin: "Plugin") -> None:
        if plugin.canonical_name() in [p.canonical_name() for p in cls._plugins]:
            raise ValueError(
                f"Plugin with name {plugin.canonical_name()} already" " registered."
            )
        cls._plugins.append(plugin)

    @classmethod
    async def get_all_plugins(cls) -> list["Plugin"]:
        return cls._plugins

    @classmethod
    async def get_plugin_by_class(cls, plugin_cls: type[Plugin]) -> "Plugin":
        for plugin in cls._plugins:
            logging.getLogger(__name__).info(
                f"Checking {plugin.__class__=} against {plugin_cls=}"
            )
            if plugin.__class__ == plugin_cls:
                return plugin

        raise ValueError(f"Plugin {plugin_cls} not found.")

    @classmethod
    async def get_plugins_for_user(cls, user: User) -> list["Plugin"]:
        plugins = []
        for plugin in cls._plugins:
            if await plugin.is_registered(user):
                plugins.append(plugin)
        return plugins

    @classmethod
    async def is_registered(cls, user: User) -> bool:
        return await redis_api.sismember(
            f"user:{user.id}:plugins", cls.canonical_name()
        )
