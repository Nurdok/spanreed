import asyncio
import datetime
import logging

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.apis.googlefit import GoogleFitApi, GoogleFitAuthenticationFlow
from spanreed.apis.obsidian import ObsidianApi


# Frontmatter property written into the daily note.
STEPS_PROPERTY = "steps"
# Folder holding the daily notes (matches the Withings integration).
DAILY_NOTE_FOLDER = "Daily"


class GoogleFitPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Google Fit"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        logging.info("Asking for Google Fit user config.")
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        if not await GoogleFitApi.is_app_configured():
            await bot.send_message(
                "Google Fit isn't set up on the server yet. An admin needs to "
                "store the OAuth client JSON in Redis under "
                "`googlefit_credentials` first."
            )
            raise RuntimeError("Google Fit app credentials not configured.")

        flow: GoogleFitAuthenticationFlow = await GoogleFitApi.start_authentication(
            user
        )
        auth_done: asyncio.Event = await flow.get_done_event()

        auth_url: str = await flow.get_auth_url()
        await bot.send_message(
            f"Click [here]({auth_url}) to authenticate with Google Fit.",
            parse_markdown=True,
        )

        await auth_done.wait()
        await bot.send_message("Google Fit authenticated successfully.")

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(text="Sync steps", callback=self._sync_now),
        )
        await super().run()

    async def run_for_user(self, user: User) -> None:
        while True:
            await self._sync(user)
            await asyncio.sleep(datetime.timedelta(hours=1).total_seconds())

    async def _sync_now(self, user: User) -> None:
        """Manually triggered sync via the Telegram command."""
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        choice = await bot.request_user_choice(
            "Sync steps for…",
            ["Today", "The last week", "Cancel"],
        )
        if choice == 0:  # Today
            days = 1
        elif choice == 1:  # The last week
            days = 7
        else:  # Cancel
            return

        await bot.notify("Syncing steps…")
        if await self._sync(user, days=days) == 0:
            await bot.notify("No new step counts found.")

    async def _sync(self, user: User, *, days: int = 1) -> int:
        """Write step counts into the daily notes for the last ``days`` days.

        Existing ``steps`` property values are left untouched. Returns the
        number of days newly written.
        """
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        fit = await GoogleFitApi.for_user(user)
        obsidian = await ObsidianApi.for_user(user)

        today = datetime.date.today()
        start = today - datetime.timedelta(days=days - 1)
        steps_by_date = await fit.get_daily_steps(start_date=start, end_date=today)
        if not steps_by_date:
            return 0

        # Only today's daily note is generated on demand; past dates are
        # expected to already have their notes.
        await obsidian.safe_generate_today_note()

        written = 0
        for date in sorted(steps_by_date):
            steps = steps_by_date[date]
            daily_note: str = await obsidian.get_daily_note(DAILY_NOTE_FOLDER, date)
            try:
                existing_value = await obsidian.get_property(daily_note, STEPS_PROPERTY)
                if existing_value is not None:
                    continue
                await obsidian.set_value_of_property(daily_note, STEPS_PROPERTY, steps)
            except FileNotFoundError:
                await bot.notify(f"No daily note for {date.isoformat()}; skipping.")
                continue

            written += 1
            await bot.notify(f"Logged steps for {date.isoformat()}: {steps}.")
        return written
