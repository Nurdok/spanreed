from dataclasses import dataclass, asdict
import logging
from todoist_api_python.api_async import TodoistAPIAsync, Task, Comment
from spanreed.apis.telegram_bot import TelegramBotApi
from typing import List
from spanreed.user import User
from spanreed.plugin import Plugin


def format_task(task):
    return f"Task(id={repr(task.id)}, content={repr(task.content)})"


@dataclass
class UserConfig:
    api_token: str


class TodoistPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls):
        return "Todoist"

    @classmethod
    def has_user_config(cls):
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    async def ask_for_user_config(self, user: User):
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        api_token = await bot.request_user_input(
            "Please enter your Todoist API token. "
        )
        await self.set_config(user, UserConfig(api_token))


class Todoist:
    def __init__(self, user_config: UserConfig):
        self._api = TodoistAPIAsync(user_config.api_token)
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "Todoist":
        return Todoist(await TodoistPlugin.get_config(user))

    async def add_task(self, **kwargs) -> Task:
        return await self._api.add_task(**kwargs)

    async def update_task(self, task: Task, **kwargs) -> bool:
        return await self._api.update_task(task.id, **kwargs)

    async def update_comment(self, comment: Comment, **kwargs) -> bool:
        return await self._api.update_comment(comment.id, **kwargs)

    async def get_first_comment_with_yaml(
        self, task: Task, create=False
    ) -> Comment:
        comments: List[Comment] = await self._api.get_comments(task_id=task.id)
        for comment in comments:
            if len(parts := comment.content.split("---")) == 3:
                return comment

        if create:
            # No yaml comment was found, create one and return it.
            comment = await self._api.add_comment(
                task_id=task.id, content="---\n---"
            )
            return comment

        raise RuntimeError(
            f"No yaml comment found for task {format_task(task)}"
        )

    async def _get_inbox_project(self):
        projects = await self._api.get_projects()
        for project in projects:
            if project.inbox_project:
                return project

    async def get_inbox_tasks(self):
        return await self._api.get_tasks(
            project_id=self._get_inbox_project().id
        )

    async def get_due_tasks(self):
        # "due before:+0 hours" means that we don't count tasks that have an
        # associated time-of-day that's in the future - this is because
        # due time-of-day usually indicates _start_ time, not due time.
        query = "overdue | (today & no time) | due before:+0 hours"
        return await self._api.get_tasks(filter=query)

    async def get_tasks_with_label(self, tag) -> List[Task]:
        # By default, only non-completed tasks are returned.
        query = f"@{tag}"
        return await self._api.get_tasks(filter=query)

    async def get_overdue_tasks_with_label(self, tag) -> List[Task]:
        query = f"@{tag} & o" f"verdue"
        return await self._api.get_tasks(filter=query)

    async def set_due_date_to_today(self, task: Task):
        self._logger.info(f"Updating {format_task(task)} due date to today")
        self._logger.info(f"{task.due=}")
        if task.due and task.due.is_recurring:
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
