import asyncio
import datetime
import json

import yaml

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.apis.withings import WithingsApi, UserConfig, MeasurementType
from spanreed.apis.obsidian import ObsidianApi
from spanreed.plugins.spanreed_monitor import suppress_and_log_exception
from dataclasses import dataclass
import logging


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

        async with WithingsApi.start_authentication(user) as auth:
            auth_done: asyncio.Event = await auth.get_done_event()

            await bot.send_message(
                f"Click [here]({auth.get_url()}) to authenticate with Withings."
            )

            await auth_done.wait()

            await WithingsPlugin.set_config(user, auth.get_user_config())

    async def run_for_user(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        withings = await WithingsApi.for_user(user)
        obsidian = await ObsidianApi.for_user(user)

        while True:
            await bot.send_message("Getting measurements.")
            measurements = await withings.get_measurements()
            if measurements:
                await bot.send_message(
                    f"Got measurements: {measurements}", parse_html=False
                )
                measurement_type_to_str = {
                    MeasurementType.WEIGHT: "weight",
                    MeasurementType.FAT_PERCENTAGE: "fat-percentage",
                    MeasurementType.FAT_MASS: "fat-mass",
                    MeasurementType.HEART_PULSE: "heart-pulse",
                }

                for measurement_type, value in measurements.items():
                    await obsidian.set_value_of_property(
                        await obsidian.get_daily_note("Daily"),
                        measurement_type_to_str[measurement_type],
                        value,
                    )

            await asyncio.sleep(datetime.timedelta(hours=1).total_seconds())
