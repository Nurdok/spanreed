import asyncio
import datetime
from spanreed.apis.todoist import Todoist, TodoistPlugin
from spanreed.user import User
from spanreed.plugin import Plugin
from spanreed.apis.telegram_bot import TelegramBotApi, TelegramBotPlugin
from typing import List, Type


class TodoistNoOverduePlugin(Plugin[None]):
    @classmethod
    def name(cls) -> str:
        return "Todoist No Overdue"

    # TODO: Make the Todoist label configurable.
    @classmethod
    def has_user_config(cls) -> bool:
        return False

    @classmethod
    def get_prerequisites(cls) -> List[Type[Plugin]]:
        return [TodoistPlugin]

    async def run_for_user(self, user: User) -> None:
        todoist_api = await Todoist.for_user(user)

        while True:
            self._logger.info(f"Updating no-overdue tasks for user {user.id}")
            await self._update_no_overdue_tasks_to_today(user, todoist_api)
            # This can't happen more than daily anyway, so every 4 hours will
            # make sure it catches overdue tasks sometime at night.
            self._logger.info(f"Waiting for 4 hours for user {user.id}")
            await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())

    async def _update_no_overdue_tasks_to_today(
        self, user: User, todoist_api: Todoist
    ) -> None:
        # TODO: Make the tag name configurable per user.
        tasks = await todoist_api.get_overdue_tasks_with_label(
            "spanreed/no-overdue"
        )
        self._logger.info(
            f"Found {len(tasks)} overdue tasks for user {user.id}"
        )
        for task in tasks:
            await todoist_api.set_due_date_to_today(task)

        self._logger.info(
            f"Updated {len(tasks)} overdue tasks to today for user {user.id}"
        )
        self._logger.info(f"{TelegramBotPlugin=}")
        if tasks and await TelegramBotPlugin.is_registered(user):
            self._logger.info(f"Sending Telegram message for user {user.id}")
            bot: TelegramBotApi = await TelegramBotApi.for_user(user)
            await bot.send_message(
                f"Updated {len(tasks)} overdue tasks to today."
            )
