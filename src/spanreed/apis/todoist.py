from dataclasses import dataclass
import logging
from typing import Any, TypeVar, Callable, Awaitable, cast
import asyncio
from functools import wraps

from todoist_api_python.api_async import TodoistAPIAsync
from todoist_api_python.models import (
    Task,
    Comment,
    Project,
)

from spanreed.user import User


T = TypeVar("T")


def with_retries(
    max_retries: int = 3, initial_delay: float = 1.0
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(
        func: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = initial_delay

            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if (
                        isinstance(e, (TodoistAPIAsync.HttpError))
                        and e.status_code == 503
                    ):
                        if attempt < max_retries - 1:
                            await asyncio.sleep(delay)
                            delay *= 2  # Exponential backoff
                            continue
                    raise

            # This line should never be reached since we either return or raise
            raise RuntimeError("Unexpected end of retry loop")

        return wrapper

    return decorator


def format_task(task: Task) -> str:
    return f"Task(id={repr(task.id)}, content={repr(task.content)})"


@dataclass
class UserConfig:
    api_token: str


class Todoist:
    def __init__(self, user_config: UserConfig):
        self._api = TodoistAPIAsync(user_config.api_token)
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "Todoist":
        # This import is here to avoid importing plugins on RPi.
        from spanreed.plugins.todoist import TodoistPlugin

        return Todoist(await TodoistPlugin.get_config(user))

    async def add_task(self, **kwargs: Any) -> Task:
        return await self._wrapped_api_call(self._api.add_task, **kwargs)

    async def update_task(self, task: Task, **kwargs: Any) -> bool:
        return await self._wrapped_api_call(
            self._api.update_task, task.id, **kwargs
        )

    async def add_comment(self, task: Task, **kwargs: Any) -> Comment:
        return await self._wrapped_api_call(
            self._api.add_comment, task.id, **kwargs
        )

    async def update_comment(self, comment: Comment, **kwargs: Any) -> bool:
        return await self._wrapped_api_call(
            self._api.update_comment, comment.id, **kwargs
        )

    async def get_first_comment_with_yaml(
        self,
        task: Task,
        create: bool = False,
    ) -> Comment:
        comments: list[Comment] = await self._wrapped_api_call(
            self._api.get_comments, task_id=task.id
        )
        for comment in comments:
            if len(parts := comment.content.split("---")) == 3:
                return comment

        if create:
            # No yaml comment was found, create one and return it.
            comment = await self._wrapped_api_call(
                self._api.add_comment, task_id=task.id, content="---\n---"
            )
            return comment

        raise RuntimeError(
            f"No yaml comment found for task {format_task(task)}"
        )

    async def _get_inbox_project(self) -> Project:
        projects = await self._wrapped_api_call(self._api.get_projects)
        for project in projects:
            if project.is_inbox_project:
                return project

        raise RuntimeError("No inbox project found")

    async def get_inbox_tasks(self) -> list[Task]:
        return await self._wrapped_api_call(
            self._api.get_tasks,
            project_id=(await self._get_inbox_project()).id,
        )

    async def get_due_tasks(self) -> list[Task]:
        # "due before:+0 hours" means that we don't count tasks that have an
        # associated time-of-day that's in the future - this is because
        # due time-of-day usually indicates _start_ time, not due time.
        query = "overdue | (today & no time) | due before:+0 hours"
        return await self._wrapped_api_call(self._api.get_tasks, filter=query)

    async def get_tasks_with_label(self, label: str) -> list[Task]:
        # By default, only non-completed tasks are returned.
        query = f"@{label}"
        return await self._wrapped_api_call(self._api.get_tasks, filter=query)

    async def get_overdue_tasks_with_label(self, label: str) -> list[Task]:
        query = f"@{label} & overdue"
        return await self._wrapped_api_call(self._api.get_tasks, filter=query)

    async def set_due_date_to_today(self, task: Task) -> None:
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
            await self._wrapped_api_call(
                self._api.update_task, task.id, due_string=task.due.string
            )
        else:
            await self._wrapped_api_call(
                self._api.update_task, task.id, due_string="today"
            )

    async def get_projects(self) -> list[Project]:
        return await self._wrapped_api_call(self._api.get_projects)

    @with_retries()
    async def _wrapped_api_call(
        self,
        api_method: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Wrapper for Todoist API calls that adds retry logic."""
        return await api_method(*args, **kwargs)
