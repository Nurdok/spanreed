import asyncio
import datetime
import os
from spanreed.apis.todoist import Todoist
import spanreed


async def _update_no_overdue_tasks_to_today(todoist_api):
    # TODO: Make the tag name configurable per user.
    tasks = await todoist_api.get_tasks_with_tag('spanreed/no-overdue')
    for task in tasks:
        await todoist_api.set_due_date_to_today(task)


class TodoistNoOverduePlugin(spanreed.plugin.Plugin):
    def __init__(self, todoist_api: Todoist, *args, **kwargs):
        self._todoist_api = todoist_api
        super().__init__(name="Todoist No Overdue", *args, **kwargs)

    async def run(self):
        while True:
            await _update_no_overdue_tasks_to_today(self._todoist_api)
            # This can't happen more than daily anyway, so every 4 hours will
            # make sure it catches overdue tasks sometime at night.
            await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())
