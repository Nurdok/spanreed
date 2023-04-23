import json
from typing import Optional, List

from spanreed.plugin import Plugin
from spanreed.user import User

from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand


class AdminPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Admin"

    @classmethod
    def has_user_config(self) -> bool:
        return False

    async def run(self):
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Manage users",
                callback=self._manage_users,
            ),
        )

    async def _manage_users(self, user: User):
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        async with bot.user_interaction():
            managed_user: User = await self._ask_for_managed_user(user, bot)
            if managed_user is None:
                return

            await self._show_user_management_options(bot, managed_user)

    async def _show_user_management_options(
        self, bot: TelegramBotApi, managed_user: User
    ):
        while True:
            choice = await bot.request_user_choice(
                f"Managing user {managed_user} What do you want to do?",
                [
                    "Change name",
                    "Change configuration",
                    "Manage plugins",
                    "Cancel",
                ],
            )

            if choice == 0:
                await self._change_name(bot, managed_user)
            elif choice == 1:
                await self._change_config(bot, managed_user)
            elif choice == 2:
                await self._manage_plugins(bot, managed_user)
            elif choice == 3:
                break

    async def _change_name(self, bot: TelegramBotApi, managed_user: User):
        await bot.send_message("The current name is: " + managed_user.name)
        choice = await bot.request_user_choice(
            "Do you want to change it?", ["Yes", "No"]
        )
        if choice == 0:
            new_name = await bot.request_user_input("What's the new name?")
            await managed_user.set_name(new_name)

    async def _change_config(self, bot: TelegramBotApi, managed_user: User):
        await bot.send_message(
            "The current config is: "
            + json.dumps(managed_user.config, indent=2)
        )
        choice = await bot.request_user_choice(
            "Do you want to change it?", ["Yes", "No"]
        )
        if choice == 0:
            new_config = await bot.request_user_input("What's the new config?")
            await managed_user.set_config(new_config)

    async def _manage_plugins(self, bot: TelegramBotApi, managed_user: User):
        while True:
            plugins = await Plugin.get_plugins_for_user(managed_user)
            if not plugins:
                await bot.send_message("The user is not using any plugins.")
            else:
                await bot.send_message(
                    "The user is currently using these plugins: \n"
                    + "\n".join(f"- {p.name}" for p in plugins)
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
                await self._register_to_new_plugin(bot, managed_user)
            elif choice == 1:  # Unregister from an existing plugin
                await self._unregister_from_an_existing_plugin(
                    bot, managed_user
                )
            elif choice == 2:  # Cancel
                return

    async def _unregister_from_an_existing_plugin(
        self, bot: TelegramBotApi, managed_user: User
    ):
        if not managed_user.plugins:
            await bot.send_message("There are no plugins to unregister from.")
            return

        plugins = await Plugin.get_plugins_for_user(managed_user)

        choice = await bot.request_user_choice(
            "Which plugin do you want to unregister from?",
            [p.name for p in plugins] + ["Cancel"],
        )

        if choice == len(plugins):  # Cancel
            return

        plugin = plugins[choice]
        self._logger.info(
            f"Unregistering user {managed_user} from plugin {plugin}"
        )
        await plugin.unregister_user(managed_user)

    async def _ask_for_managed_user(
        self, user: User, bot: TelegramBotApi
    ) -> Optional[User]:
        choice = await bot.request_user_choice(
            "Which user do you need to manage?",
            ["Me", "Another existing user", "Create a new user"],
        )

        managed_user: Optional[User] = None

        if choice == 0:
            managed_user = user
        elif choice == 1:
            managed_user = await self._ask_for_another_existing_user(user, bot)
        elif choice == 2:
            managed_user = await self._create_user(bot)

        if managed_user is None:
            return None

        return managed_user

    async def _create_user(self, bot: TelegramBotApi) -> User:
        name = await bot.request_user_input("What's the name of the user?")
        return await User.create(name)

    async def _ask_for_another_existing_user(
        self, user: User, bot: TelegramBotApi
    ) -> Optional[User]:
        choice = await bot.request_user_choice(
            "How do you want to find the user?",
            ["Enter ID", "Browse"],
        )

        if choice == 0:
            managed_user_id = await bot.request_user_input("What's the ID?")
            return await User.find_by_id(managed_user_id)
        elif choice == 1:
            return await self._browse_for_user(user, bot)

    async def _browse_for_user(
        self, user: User, bot: TelegramBotApi
    ) -> Optional[User]:
        users: List[User] = await User.get_all_users(redis_api=self._redis)
        self._logger.info(f"Found {len(users)} users")

        while users:
            # Use pagination to show 5 users at a time.
            current_users = users[:5]
            users = users[5:]
            options = [f"{user.name} ({user.id})" for user in current_users]
            if users:
                options.append("Next")
            options.append("Cancel")

            choice = await bot.request_user_choice(
                "Which user do you want to manage?", options
            )
            if choice == len(options):  # Cancel
                return None
            elif users and choice == len(options) - 1:  # Next
                continue
            else:
                return current_users[choice]
