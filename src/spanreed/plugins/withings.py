import asyncio
import datetime

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.apis.withings import (
    WithingsApi,
    UserConfig,
    MeasurementType,
    AuthenticationFlow,
)
from spanreed.apis.obsidian import ObsidianApi
import logging


MEASUREMENT_TYPE_TO_STR = {
    MeasurementType.WEIGHT: "weight",
    MeasurementType.FAT_PERCENTAGE: "fat-percentage",
    MeasurementType.FAT_MASS: "fat-mass",
    MeasurementType.HEART_PULSE: "heart-pulse",
}


class WithingsPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Withings"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig] | None:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        logging.info("Asking for Withings user config.")
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        auth: AuthenticationFlow
        async with WithingsApi.start_authentication(user) as auth:
            auth_done: asyncio.Event = await auth.get_done_event()

            await bot.send_message(
                f"Click [here]({auth.get_url()}) to authenticate with Withings.",
                parse_markdown=True,
            )

            await auth_done.wait()

            await WithingsPlugin.set_config(user, auth.get_user_config())

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(text="Sync Withings data", callback=self._sync_now),
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
            "Sync Withings data for…",
            ["Today", "The last week", "Cancel"],
        )
        if choice == 0:  # Today
            days = 1
        elif choice == 1:  # The last week
            days = 7
        else:  # Cancel
            return

        await bot.send_message("Syncing Withings data…")
        if await self._sync(user, days=days) == 0:
            await bot.send_message("No new Withings measurements found.")

    async def _sync(self, user: User, *, days: int = 1) -> int:
        """Write measurements into the daily notes for the last ``days`` days.

        Existing property values are left untouched. Returns the number of
        measurement values newly written.
        """
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        withings = await WithingsApi.for_user(user)
        obsidian = await ObsidianApi.for_user(user)

        today = datetime.date.today()
        start = today - datetime.timedelta(days=days - 1)
        measurements_by_date = await withings.get_measurements(
            start_date=start, end_date=today
        )
        if not measurements_by_date:
            return 0

        # Only today's daily note is generated on demand; past dates are
        # expected to already have their notes.
        await obsidian.safe_generate_today_note()

        written = 0
        for date in sorted(measurements_by_date):
            daily_note: str = await obsidian.get_daily_note("Daily", date)
            for measurement_type, value in measurements_by_date[date].items():
                name = MEASUREMENT_TYPE_TO_STR[measurement_type]
                try:
                    existing_value = await obsidian.get_property(
                        daily_note, name
                    )
                    if existing_value is not None:
                        continue
                    await obsidian.set_value_of_property(
                        daily_note, name, value
                    )
                except FileNotFoundError:
                    await bot.send_message(
                        f"No daily note for {date.isoformat()}; skipping."
                    )
                    break

                written += 1
                await bot.send_message(
                    f"Logged {name} for {date.isoformat()}: {value}."
                )
        return written
