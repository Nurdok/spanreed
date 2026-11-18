import asyncio
import datetime

from spanreed.plugin import Plugin
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.interaction import UserInteractionApi, PluginCommand
from spanreed.daily_note_fields import ask_for_daily_note_fields
from spanreed.apis.withings import (
    WithingsApi,
    DailyNoteFields,
    UserConfig,
    MeasurementType,
    AuthenticationFlow,
)
from spanreed.apis.obsidian import ObsidianApi
import logging


# How long Spanreed remembers that it has handled a measurement. The manual
# sync reaches back a week, so this only has to be longer than that.
HANDLED_TTL = datetime.timedelta(days=90)


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
        """Log in to Withings and choose the daily note properties.

        A user who is already logged in can keep the login, so that the
        properties can be changed without logging in again.
        """
        logging.info("Asking for Withings user config.")
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)

        config: UserConfig
        if await cls.get_saved_config_data(user) is not None:
            config = await cls.get_config(user)
            choice = await bot.request_user_choice(
                "You are logged in to Withings.",
                ["Keep the current login", "Log in again"],
            )
            if choice == 1:  # Log in again
                config = await cls._authenticate(user, bot, config.daily_note_fields)
        else:
            config = await cls._authenticate(user, bot, DailyNoteFields())

        await ask_for_daily_note_fields(bot, config.daily_note_fields)
        # Take the tokens as they are now: a sync may have refreshed them
        # while the user was choosing.
        saved = await cls.get_saved_config_data(user)
        if saved is not None:
            fields = config.daily_note_fields
            config = await cls.get_config(user)
            config.daily_note_fields = fields
        await cls.set_config(user, config)

    @classmethod
    async def _authenticate(
        cls, user: User, bot: UserInteractionApi, fields: DailyNoteFields
    ) -> UserConfig:
        """Run the Withings login and save the config it gives.

        Keep ``fields`` as the daily note properties of the new config.
        """
        auth: AuthenticationFlow
        async with WithingsApi.start_authentication(user) as auth:
            auth_done: asyncio.Event = await auth.get_done_event()

            await bot.send_message(
                f"Click [here]({auth.get_url()}) to authenticate with Withings.",
                parse_markdown=True,
            )

            await auth_done.wait()

            config = auth.get_user_config()
            config.daily_note_fields = fields
            await cls.set_config(user, config)
            return config

    async def run(self) -> None:
        await UserInteractionApi.register_command(
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
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        if not config.daily_note_fields.written():
            await bot.send_message(
                "Every Withings property is turned off in the Withings settings."
            )
            return
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

        await bot.notify("Syncing Withings data…")
        if await self._sync(user, days=days, report_removed=True) == 0:
            await bot.notify("No new Withings measurements found.")

    @staticmethod
    def _handled_key(user: User, date: datetime.date) -> str:
        return f"withings:{user.id}:handled:{date.isoformat()}"

    @classmethod
    async def _is_handled(cls, user: User, date: datetime.date, marker: str) -> bool:
        """Return whether this measurement was in the note before."""
        return bool(await redis_api.sismember(cls._handled_key(user, date), marker))

    @classmethod
    async def _mark_handled(cls, user: User, date: datetime.date, marker: str) -> None:
        """Remember that this measurement is, or was, in the note."""
        key = cls._handled_key(user, date)
        await redis_api.sadd(key, marker)
        await redis_api.expire(key, int(HANDLED_TTL.total_seconds()))

    async def _sync(
        self, user: User, *, days: int = 1, report_removed: bool = False
    ) -> int:
        """Write measurements into the daily notes for the last ``days`` days.

        Write only the properties that the user has turned on. Existing
        property values are left untouched. Returns the number of
        measurement values newly written.

        Write each measurement to a note once only. Withings goes on
        returning a measurement after the user deletes it in the Withings
        app, so a property that the user removes from the note would
        otherwise come back with the next sync. A measurement whose property
        is gone from the note is left out; if ``report_removed`` is true,
        tell the user how many were.
        """
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        fields = config.daily_note_fields
        if not fields.written():
            return 0
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
        removed = 0
        for date in sorted(measurements_by_date):
            daily_note: str = await obsidian.get_daily_note("Daily", date)
            for measurement_type, value in measurements_by_date[date].items():
                name = MEASUREMENT_TYPE_TO_STR[measurement_type]
                if not fields.writes(name):
                    continue
                # The value identifies the measurement: a new reading on the
                # same day has another value and is written.
                marker = f"{name}={value!r}"
                try:
                    existing_value = await obsidian.get_property(daily_note, name)
                    if existing_value is not None:
                        await self._mark_handled(user, date, marker)
                        continue
                    if await self._is_handled(user, date, marker):
                        removed += 1
                        self._logger.info(
                            f"Not writing {name} for {date.isoformat()} again: "
                            "it was removed from the note."
                        )
                        continue
                    await obsidian.set_value_of_property(daily_note, name, value)
                except FileNotFoundError:
                    await bot.notify(f"No daily note for {date.isoformat()}; skipping.")
                    break

                await self._mark_handled(user, date, marker)
                written += 1
                await bot.notify(f"Logged {name} for {date.isoformat()}: {value}.")
        if removed and report_removed:
            await bot.notify(
                f"Left out {removed} "
                f"{'measurement' if removed == 1 else 'measurements'} that you "
                "removed from your notes."
            )
        return written
