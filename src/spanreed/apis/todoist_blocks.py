"""The Todoist blocks for workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from spanreed.apis.todoist import Todoist
from spanreed.workflows.blocks import Action, Block, StepContext


@dataclass
class FindTasksInput:
    """Which tasks to find."""

    # Without the "@".
    label: str
    due: Literal["any", "overdue"] = "any"


@dataclass
class FindTasksOutput:
    """The tasks that were found."""

    tasks: list[Any]
    count: int


class FindTasks(Action[FindTasksInput, FindTasksOutput]):
    """Find the open tasks that have a label."""

    id = "todoist.find-tasks"
    title = "Find tasks"

    async def run(self, ctx: StepContext, inputs: FindTasksInput) -> FindTasksOutput:
        """Return the tasks with the label, or only the overdue ones."""
        todoist = await Todoist.for_user(ctx.user)
        if inputs.due == "overdue":
            tasks = await todoist.get_overdue_tasks_with_label(inputs.label)
        else:
            tasks = await todoist.get_tasks_with_label(inputs.label)
        return FindTasksOutput(tasks=tasks, count=len(tasks))


@dataclass
class SetDueDateInput:
    """Which tasks to move, and to when."""

    # Tasks as another Todoist block gave them.
    tasks: list[Any]
    due: Literal["today"] = "today"


@dataclass
class SetDueDateOutput:
    """How many tasks were moved."""

    count: int


class SetDueDate(Action[SetDueDateInput, SetDueDateOutput]):
    """Move tasks to a due date. A recurring task keeps its recurrence."""

    id = "todoist.set-due-date"
    title = "Set due date"

    async def run(self, ctx: StepContext, inputs: SetDueDateInput) -> SetDueDateOutput:
        """Set the due date of every task to today."""
        todoist = await Todoist.for_user(ctx.user)
        for task in inputs.tasks:
            await todoist.set_due_date_to_today(task)
        return SetDueDateOutput(count=len(inputs.tasks))


def todoist_blocks() -> list[Block]:
    """Return the blocks that the Todoist plugin publishes."""
    return [FindTasks(), SetDueDate()]
