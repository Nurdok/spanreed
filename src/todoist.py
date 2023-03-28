import logging
from todoist_api_python.api_async import TodoistAPIAsync, Task


def format_task(task):
    return f'Task(id={repr(task.id)}, content={repr(task.content)})'


class Todoist:
    def __init__(self, api_token):
        self._api = TodoistAPIAsync(api_token)
        self._logger = logging.getLogger(__name__)

    async def _get_inbox_project(self):
        projects = await self._api.get_projects()
        for project in projects:
            if project.inbox_project:
                return project

    async def get_inbox_tasks(self):
        return await self._api.get_tasks(project_id=self._get_inbox_project().id)

    async def get_due_tasks(self):
        # "due before:+0 hours" means that we don't count tasks that have an
        # associated time-of-day that's in the future - this is because
        # due time-of-day usually indicates _start_ time, not due time.
        query = "overdue | (today & no time) | due before:+0 hours"
        return await self._api.get_tasks(filter=query)

    async def get_tasks_with_tag(self, tag):
        # By default, only non-completed tasks are returned.
        query = f'@{tag}'
        return await self._api.get_tasks(filter=query)

    async def set_due_date_to_today(self, task: Task):
        self._logger.info(f'Updating {format_task(task)} due date to today')
        self._logger.info(f'{task.due=}')
        if task.due.is_recurring:
            # For recurring tasks, we need to keep the recurrence. The string
            # looks something like "daily", "every week" or "on the 1st of
            # every month". When a task recurrence is edited, the next
            # occurrence is always set to the next possible slot, usually
            # today.
            # If we have an overdue task with due string of "every week"
            # and we re-enter this same due string, it'll make the current
            # occurrence with a due date of today.
            await self._api.update_task(task.id, due_string=task.due.string)
        else:
            await self._api.update_task(task.id, due_string="today")
