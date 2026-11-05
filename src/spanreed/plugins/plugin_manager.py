import datetime
from typing import List

from spanreed.interaction import UserInteractionApi, PluginCommand
from spanreed.plugin import Plugin
from spanreed.user import User


async def ask_for_timezone(user: User, bot: UserInteractionApi) -> bool:
    """Ask the user for their IANA timezone name and store it.

    Return True if a valid name was stored. Return False if the user gave
    an unknown name; the user's timezone is then unchanged. Plugins that
    need the timezone during setup can call this when it is not set yet.
    """
    current = user.timezone or "not set"
    name = await bot.request_user_input(
        f"Your timezone is {current}.\n"
        "Enter an IANA timezone name (for example Asia/Jerusalem or "
        "America/New_York):"
    )
    try:
        await user.set_timezone(name)
    except ValueError:
        await bot.send_message(
            f"'{name.strip()}' is not a known timezone. "
            "Use a name from the IANA database, such as Europe/London."
        )
        return False
    now = datetime.datetime.now(tz=user.tzinfo)
    await bot.send_message(
        f"Timezone set to {user.timezone}. "
        f"Your local time is now {now.strftime('%H:%M')}."
    )
    return True


class PluginManagerPlugin(Plugin):
    """A plugin that manages user registration for other plugins."""

    @classmethod
    def name(cls) -> str:
        return "Plugin Manager"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    async def run(self) -> None:
        await UserInteractionApi.register_command(
            self,
            PluginCommand(
                text="Manage plugins",
                callback=self._manage_plugins,
            ),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(
                text="Set timezone",
                callback=self._set_timezone,
            ),
        )

    async def _set_timezone(self, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        await ask_for_timezone(user, bot)

    async def _manage_plugins(self, user: User) -> None:
        self._logger.info(f"Managing plugins for user {user.id}")
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)

        while True:
            plugins = await Plugin.get_plugins_for_user(user)
            if not plugins:
                await bot.send_message("You are not using any plugins.")
            else:
                await bot.send_message(
                    f"You are currently using these plugins,"
                    f" {user.name}: \n" + "\n".join(f"- {p.name()}" for p in plugins)
                )

            choice = await bot.request_user_choice(
                "What would you like to do?",
                [
                    "Register to new plugin",
                    "Unregister from an existing plugin",
                    "Reconfigure an existing plugin",
                    "Cancel",
                ],
            )
            if choice == 0:  # Register to new plugin
                await self._register_to_new_plugin(bot, user)
            elif choice == 1:  # Unregister from an existing plugin
                await self._unregister_from_an_existing_plugin(bot, user)
            elif choice == 2:  # Reconfigure an existing plugin
                await self._reconfigure_existing_plugin(bot, user)
            elif choice == 3:  # Cancel
                break

    async def _register_to_new_plugin(
        self, bot: UserInteractionApi, user: User
    ) -> None:
        # Filter out plugins that the user is already using.
        plugins: List[Plugin] = [
            plugin
            for plugin in await Plugin.get_all_plugins()
            if user.plugins is None or plugin.canonical_name() not in user.plugins
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
        self, bot: UserInteractionApi, user: User
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

    async def _reconfigure_existing_plugin(
        self, bot: UserInteractionApi, user: User
    ) -> None:
        plugins = [
            plugin
            for plugin in await Plugin.get_plugins_for_user(user)
            if plugin.has_user_config()
        ]

        if not plugins:
            await bot.send_message("There are no plugins to reconfigure.")
            return

        if (
            choice := await bot.request_user_choice(
                "Which plugin do you want to reconfigure?",
                [p.name() for p in plugins] + ["Cancel"],
            )
        ) == len(plugins):
            return

        plugin = plugins[choice]
        self._logger.info(f"Reconfiguring user {user} for plugin {plugin}")
        await plugin.ask_for_user_config(user)
