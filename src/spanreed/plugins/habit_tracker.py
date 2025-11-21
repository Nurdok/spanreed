import asyncio
import datetime
from dataclasses import dataclass
from typing import Any
from contextlib import suppress

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import (
    PluginCommand,
    TelegramBotApi,
    UserInteractionPriority,
    UserInteractionPreempted,
)
from spanreed.apis.obsidian import ObsidianApi, ObsidianApiTimeoutError
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


def time_until_end_of_day() -> datetime.timedelta:
    """
    Get timedelta until end of day on the datetime passed, or current time.
    """
    now = datetime.datetime.now()
    tomorrow = now + datetime.timedelta(days=1)
    return datetime.datetime.combine(tomorrow, datetime.time.min) - now


class HabitTrackerPlugin(Plugin):

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Track Habits",
                callback=self.poll_user_for_all_habits,
            ),
        )

        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Add Habit",
                callback=self.add_habit,
            ),
        )

        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Remove Habit",
                callback=self.remove_habit,
            ),
        )

        await super().run()

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
        done_habits: list[str] = await self.get_done_habits(user)
        return [
            habit for habit in config.habits if habit.name not in done_habits
        ]

    async def mark_habit_as_done(self, user: User, habit_name: str) -> None:
        config: UserConfig = await self.get_config(user)
        obsidian: ObsidianApi = await ObsidianApi.for_user(user)
        await obsidian.add_value_to_list_property(
            filepath=await obsidian.get_daily_note(config.daily_note_path),
            property_name=config.habit_tracker_property_name,
            value=habit_name,
        )

    async def poll_user_for_all_habits(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        self._logger.info(f"Running periodic check for user {user}")
        async with suppress_and_log_exception(ObsidianApiTimeoutError):
            habits: list[Habit] = await self.get_habits_to_poll(user)
            self._logger.info(f"Found {len(habits)} habits: {habits}")
            if not habits:
                return

            while habits:
                choices: list[str | list[str]] = [
                    habit.name.capitalize() for habit in habits
                ] + [["Cancel"]]
                choice: int = await bot.request_user_choice(
                    "Did you do any of these habits today?",
                    choices,
                    columns=max(4, len(habits)),
                )
                if choice == len(habits):
                    return
                habit: Habit = habits[choice]
                await self.mark_habit_as_done(user, habit.name)
                habits.remove(habit)

    async def add_habit(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        config: UserConfig = await self.get_config(user)

        habit_name: str = await bot.request_user_input(
            "What is the name of the habit you want to add?"
        )

        if any(
            habit.name.lower() == habit_name.lower() for habit in config.habits
        ):
            await bot.send_message(f"Habit '{habit_name}' already exists!")
            return

        habit_description: str = await bot.request_user_input(
            "How would you describe this habit?"
        )

        new_habit = Habit(habit_name, habit_description)
        config.habits.append(new_habit)

        await self.set_config(user, config)
        await bot.send_message(f"Added habit: {habit_name}")

    async def remove_habit(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        config: UserConfig = await self.get_config(user)

        if not config.habits:
            await bot.send_message("You don't have any habits to remove!")
            return

        habit_choices: list[str] = [habit.name for habit in config.habits] + [
            "Cancel"
        ]
        choice: int = await bot.request_user_choice(
            "Which habit would you like to remove?",
            habit_choices,
            columns=max(2, len(config.habits)),
        )

        if choice == len(config.habits):
            await bot.send_message("Cancelled.")
            return

        removed_habit = config.habits.pop(choice)
        await self.set_config(user, config)
        await bot.send_message(f"Removed habit: {removed_habit.name}")

    async def run_for_user(self, user: User) -> None:
        self._logger.info(f"Running for user {user}")
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)

        try:
            while True:
                self._logger.info(f"Polling user {user}")
                eod_timeout = time_until_end_of_day().total_seconds()
                one_hour = 60 * 60
                min_timeout = min(eod_timeout, one_hour)
                with suppress(TimeoutError):
                    async with asyncio.timeout(min_timeout):
                        try:
                            async with bot.user_interaction(
                                priority=UserInteractionPriority.LOW,
                                propagate_preemption=True,
                            ):
                                self._logger.info("Got user interaction lock")
                                await self.poll_user_for_all_habits(user)
                        except UserInteractionPreempted:
                            self._logger.info(
                                "User interaction preempted, trying again"
                            )
                        else:
                            self._logger.info("Sleeping for 4 hours")
                            await asyncio.sleep(
                                datetime.timedelta(hours=4).total_seconds()
                            )
                self._logger.info("Passed midnight, re-asking")
        except Exception:
            self._logger.exception("Error in run_for_user")
            raise
        finally:
            self._logger.info("Exiting run_for_user")
