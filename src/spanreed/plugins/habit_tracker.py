import asyncio
import datetime
from dataclasses import dataclass
from typing import Any

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.apis.obsidian import ObsidianApi


@dataclass
class Habit:
    name: str
    description: str
    # TODO: Add a way to specify the frequency of the habit


@dataclass
class UserConfig:
    habit_tracker_property_name: str
    habits: list[Habit]

    def __post_init__(self) -> None:
        for index, habit in enumerate(self.habits):
            if isinstance(habit, dict):
                self.habits[index] = Habit(**habit)


class HabitTrackerPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Habit Tracker"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig] | None:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        habit_tracker_property_name: str = await bot.request_user_input(
            "What is the name of the property that you use to track your habits?"
        )

        habits: list[Habit] = []
        while True:
            habit_name: str = await bot.request_user_input(
                "What is the name of the habit you want to track?"
            )
            habit_description: str = await bot.request_user_input(
                "How would you describe this habit?"
            )
            habits.append(Habit(habit_name, habit_description))
            if await bot.request_user_choice(
                "Do you want to add another habit?", ["Yes", "No"]
            ):
                break

        await cls.set_config(
            user,
            UserConfig(
                habit_tracker_property_name=habit_tracker_property_name,
                habits=habits,
            ),
        )

    async def run_for_user(self, user: User) -> None:
        self._logger.info(f"Running for user {user}")
        config: UserConfig = await self.get_config(user)
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        while True:
            property_value: Any = await obsidian.get_property(
                await obsidian.get_daily_note_for(datetime.date.today()),
                config.habit_tracker_property_name,
            )
            done_habits: list[str] = []

            if isinstance(property_value, list):
                done_habits = property_value
            elif property_value is not None:
                async with bot.user_interaction():
                    await bot.send_message(
                        f"Invalid value for {config.habit_tracker_property_name}: {property_value!r}"
                        f"; expected a list of strings."
                    )

            for habit in config.habits:
                self._logger.info(
                    f"Checking if we need to ask for {habit.name}"
                )
                if habit.name in done_habits:
                    self._logger.info(f"{habit.name} is already done")
                    continue

                await self.poll_user(habit, bot, obsidian)

            await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())

    async def poll_user(
        self, habit: Habit, bot: TelegramBotApi, obsidian: ObsidianApi
    ) -> None:
        async with bot.user_interaction():
            self._logger.info(f"Polling user for {habit.name}")
            prompt = f"Did you {habit.description} today?"
            if await bot.request_user_choice(prompt, ["Yes", "No"]):
                self._logger.info(f"User said yes to {habit.name}")
                await obsidian.add_value_to_list_property(
                    await obsidian.get_daily_note_for(datetime.date.today()),
                    habit.name,
                    habit.name,
                )
                await bot.send_message(f"Awesome! Keep it up!")
            else:
                self._logger.info(f"User said no to {habit.name}")
                await bot.send_message("I'll ask again later")
