import asyncio
import datetime
from dataclasses import dataclass
from typing import Any
from contextlib import suppress

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import (
    TelegramBotApi,
    UserInteractionPriority,
    UserInteractionPreempted,
)
from spanreed.apis.obsidian import ObsidianApi
from spanreed.plugins.spanreed_monitor import suppress_and_log_exception


@dataclass
class Habit:
    name: str
    description: str
    # TODO: Add a way to specify the frequency of the habit


@dataclass
class UserConfig:
    daily_note_path: str
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

        daily_note_path: str = await bot.request_user_input(
            "What is the path to your daily notes?"
        )

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
                daily_note_path=daily_note_path,
                habit_tracker_property_name=habit_tracker_property_name,
                habits=habits,
            ),
        )

    async def get_habit_tracker_property_value(
        self,
        obsidian: ObsidianApi,
        bot: TelegramBotApi,
        property_name: str,
        daily_note_path: str,
    ) -> Any:
        await obsidian.safe_generate_today_note()

        async def fetch_value() -> Any:
            return await obsidian.get_property(
                await obsidian.get_daily_note(daily_note_path),
                property_name,
            )

        try:
            return await fetch_value()
        except FileNotFoundError:
            await bot.send_message("Generating today's daily note...")
            await obsidian.safe_generate_today_note()
            return await fetch_value()

    async def get_done_habits(self, user: User) -> list[str]:
        config: UserConfig = await self.get_config(user)
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        property_value: Any = await self.get_habit_tracker_property_value(
            obsidian,
            bot,
            config.habit_tracker_property_name,
            config.daily_note_path,
        )
        done_habits: list[str] = []

        if isinstance(property_value, list):
            done_habits = property_value
        elif property_value is not None:
            async with bot.user_interaction():
                await bot.send_message(
                    f"Invalid value for "
                    f"{config.habit_tracker_property_name}: "
                    f"{property_value!r}"
                    f"; expected a list of strings."
                )
        return done_habits

    async def get_habits_to_poll(self, user: User) -> list[Habit]:
        config: UserConfig = await self.get_config(user)
        return [
            habit
            for habit in config.habits
            if habit.name not in await self.get_done_habits(user)
        ]

    async def mark_habit_as_done(self, user: User, habit_name: str) -> None:
        config: UserConfig = await self.get_config(user)
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        await obsidian.add_value_to_list_property(
            await obsidian.get_daily_note(config.daily_note_path),
            config.habit_tracker_property_name,
            habit_name,
        )

    async def poll_user_for_all_habits(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        self._logger.info(f"Running periodic check for user {user}")
        async with suppress_and_log_exception(TimeoutError):
            habits: list[Habit] = await self.get_habits_to_poll(user)
            for habit in habits:
                if await self.poll_user_for_habit(habit, bot):
                    await self.mark_habit_as_done(user, habit.name)
                    await bot.send_message(f"Awesome! Keep it up!")
                else:
                    await bot.send_message("I'll ask again later")

    async def run_for_user(self, user: User) -> None:
        self._logger.info(f"Running for user {user}")
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        while True:
            self._logger.info(f"Polling user {user}")
            try:
                async with bot.user_interaction(
                    priority=UserInteractionPriority.LOW,
                    propagate_preemption=True,
                ):
                    self._logger.info("Got user interaction lock")
                    await self.poll_user_for_all_habits(user)
            except UserInteractionPreempted:
                self._logger.info("User interaction preempted, trying again")
            else:
                self._logger.info("Sleeping for 4 hours")
                await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())

    async def poll_user_for_habit(
        self, habit: Habit, bot: TelegramBotApi
    ) -> bool:
        self._logger.info(f"Polling user for {habit.name}")
        prompt = f"Did you {habit.description} today?"
        if await bot.request_user_choice(prompt, ["Yes", "No"]) == 0:
            self._logger.info(f"User said yes to {habit.name}")
            return True
        self._logger.info(f"User said no to {habit.name}")
        return False
