import asyncio
import datetime
import os
from todoist import Todoist


async def _update_no_overdue_tasks_to_today(todoist_api):
    tasks = await todoist_api.get_tasks_with_tag('spanreed/no-overdue')
    for task in tasks:
        await todoist_api.set_due_date_to_today(task)


async def main(todoist_api):
    while True:
        await _update_no_overdue_tasks_to_today(todoist_api)
        # This can't happen more than daily anyway, so every 4 hours will make
        # sure it catches overdue tasks sometime at night.
        await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())


if __name__ == '__main__':
    asyncio.run(main(Todoist(os.environ['TODOIST_API_TOKEN'])))



