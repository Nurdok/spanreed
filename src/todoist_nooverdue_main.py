import asyncio
import datetime
import os
from todoist import Todoist
import logging
import sys


def log_to_stdout():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)


async def _update_no_overdue_tasks_to_today(todoist_api):
    tasks = await todoist_api.get_tasks_with_tag('spanreed/no-overdue')
    for task in tasks:
        await todoist_api.set_due_date_to_today(task)


async def main(todoist_api):
    while True:
        await _update_no_overdue_tasks_to_today(todoist_api)
        await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())


if __name__ == '__main__':
    asyncio.run(main(Todoist(os.environ['TODOIST_API_TOKEN'])))



