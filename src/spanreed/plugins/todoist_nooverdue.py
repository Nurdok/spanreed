import asyncio
import datetime
from spanreed.apis.todoist import Todoist, TodoistPlugin
from spanreed.user import User
from spanreed.plugin import Plugin
from typing import List


async def _update_no_overdue_tasks_to_today(todoist_api: Todoist):
    # TODO: Make the tag name configurable per user.
    tasks = await todoist_api.get_overdue_tasks_with_label(
        "spanreed/no-overdue"
    )
    for task in tasks:
        await todoist_api.set_due_date_to_today(task)


class TodoistNoOverduePlugin(Plugin):
    @property
    def name(self) -> str:
        return "Todoist No Overdue"

    # TODO: Make the Todoist label configurable.
    def has_user_config(self) -> bool:
        return False

    @classmethod
    def get_prerequisites(cls) -> List[type("Plugin")]:
        return [TodoistPlugin]

    async def run_for_user(self, user: User):
        todoist_api = Todoist.for_user(user)

        while True:
            self._logger.info(f"Updating no-overdue tasks for user {user.id}")
            await _update_no_overdue_tasks_to_today(todoist_api)
            # This can't happen more than daily anyway, so every 4 hours will
            # make sure it catches overdue tasks sometime at night.
            self._logger.info(f"Waiting for 4 hours for user {user.id}")
            await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())
