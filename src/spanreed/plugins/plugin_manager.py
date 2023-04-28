from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.plugin import Plugin
from spanreed.user import User

from typing import List

import redis.asyncio as redis


class PluginManagerPlugin(Plugin):
    """A plugin that manages user registration for other plugins."""

    def __init__(self, redis_api: redis.Redis):
        super().__init__(redis_api)

    @classmethod
    def name(cls) -> str:
        return "Plugin Manager"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Manage plugins",
                callback=self._manage_plugins,
            ),
        )

    async def _manage_plugins(self, user: User) -> None:
        self._logger.info(f"Managing plugins for user {user.id}")
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        async with bot.user_interaction():
            while True:
                plugins = await Plugin.get_plugins_for_user(user)
                if not plugins:
                    await bot.send_message("You are not using any plugins.")
                else:
                    await bot.send_message(
                        f"You are currently using these plugins,"
                        f" {user.name}: \n"
                        + "\n".join(f"- {p.name()}" for p in plugins)
                    )

                choice = await bot.request_user_choice(
                    "What would you like to do?",
                    [
                        "Register to new plugin",
                        "Unregister from an existing plugin",
                        "Cancel",
                    ],
                )
                if choice == 0:  # Register to new plugin
                    await self._register_to_new_plugin(bot, user)
                elif choice == 1:  # Unregister from an existing plugin
                    await self._unregister_from_an_existing_plugin(bot, user)
                elif choice == 2:  # Cancel
                    break

    async def _register_to_new_plugin(
        self, bot: TelegramBotApi, user: User
    ) -> None:
        # Filter out plugins that the user is already using.
        plugins: List[Plugin] = [
            plugin
            for plugin in await Plugin.get_all_plugins()
            if user.plugins is None
            or plugin.canonical_name() not in user.plugins
        ]

        if not plugins:
            await bot.send_message("There are no plugins to register to.")
            return

        choice = await bot.request_user_choice(
            "Which plugin do you want to register to?",
            [p.name() for p in plugins] + ["Cancel"],
        )
        if choice == len(plugins):  # Cancel
            return

        plugin = plugins[choice]
        self._logger.info(f"Registering user {user} to plugin {plugin}")
        await plugin.register_user(user)

    async def _unregister_from_an_existing_plugin(
        self, bot: TelegramBotApi, user: User
    ) -> None:
        if not user.plugins:
            await bot.send_message("There are no plugins to unregister from.")
            return

        plugins = await Plugin.get_plugins_for_user(user)

        choice = await bot.request_user_choice(
            "Which plugin do you want to unregister from?",
            [p.name() for p in plugins] + ["Cancel"],
        )

        if choice == len(plugins):  # Cancel
            return

        plugin = plugins[choice]
        self._logger.info(f"Unregistering user {user} from plugin {plugin}")
        await plugin.unregister_user(user)
