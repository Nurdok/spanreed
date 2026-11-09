"""Settings about the user as a person, not about any one plugin."""

import datetime

from spanreed.interaction import UserInteractionApi, PluginCommand
from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.web import auth as web_auth


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


class UserProfilePlugin(Plugin):
    """Core plugin that owns the commands for the user's own settings."""

    @classmethod
    def name(cls) -> str:
        return "User Profile"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    async def run(self) -> None:
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Set timezone", callback=self._set_timezone),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Web UI login link", callback=self._web_login_link),
        )
        await self._register_existing_users()

    async def _register_existing_users(self) -> None:
        """Register every user to this core plugin.

        New users get it at /start, but users who signed up before this
        plugin existed don't, so they never saw its commands ("Set
        timezone", "Web UI login link"). Every Spanreed user is a Telegram
        user, so the Telegram plugin's user list is the full list.
        """
        from spanreed.apis.telegram_bot import TelegramBotPlugin

        telegram = await Plugin.get_plugin_by_class(TelegramBotPlugin)
        for user in await telegram.get_users():
            if not await self.is_registered(user):
                self._logger.info(f"Registering existing user {user.id}")
                await self.register_user(user)

    async def _set_timezone(self, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        await ask_for_timezone(user, bot)

    async def _web_login_link(self, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        link = await web_auth.create_login_link(user.id)
        await bot.send_message(
            "Open this link to log in to the Spanreed web UI, where you can"
            " run the same commands as here. It works once and expires in an"
            f' hour; the login then lasts 90 days.\n<a href="{link}">{link}</a>'
        )
