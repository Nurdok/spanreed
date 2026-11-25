"""The Todoist blocks for workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from spanreed.apis.todoist import Todoist
from spanreed.config_form import HELP_KEY, described
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

    tasks: list[Any] = described(
        "The tasks that were found. “Set due date” takes them",
    )
    count: int = described("How many tasks were found")


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

    count: int = described("How many tasks were moved")


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


# -- a tally of dates on a task ------------------------------------------------
#
# One task stands for something that comes up again and again and is paid
# for in one go: the sessions with a therapist. The task is found by a label
# that only it has. Its dates are kept as YAML in a comment of the task,
# between two lines of "---", and the name of the task says what is owed.


async def _tally_task(todoist: Todoist, label: str) -> Any | None:
    """Return the one task that has the label, or ``None`` if there is none."""
    tasks = await todoist.get_tasks_with_label(label)
    if len(tasks) > 1:
        raise RuntimeError(
            f"Expected either zero or exactly one task with the label"
            f" {label}, got {len(tasks)}"
        )
    return tasks[0] if tasks else None


def _tally_of(comment_text: str) -> tuple[list[str], dict[str, Any]]:
    """Return the dates in a comment, and everything its YAML holds."""
    parts = comment_text.split("---")
    if len(parts) != 3:
        raise RuntimeError("The comment with the dates is not as it was written.")
    held = yaml.safe_load(parts[1]) or {}
    dates = held.get("dates") if isinstance(held, dict) else None
    return [str(date) for date in dates or []], held if isinstance(held, dict) else {}


@dataclass
class ReadTallyInput:
    """Which task, which date, and what one date costs."""

    label: str = field(metadata={HELP_KEY: "A label that only this task has"})
    # The date that is about to be added: 2026-11-24.
    date: str
    cost: float = field(
        default=0, metadata={HELP_KEY: "What one date costs. 0: only the dates count"}
    )


@dataclass
class ReadTallyOutput:
    """The dates on the task, and what they would be with the date."""

    found: bool = described("Whether there is a task with the label")
    has_date: bool = described("Whether the task has the date already")
    dates: list[str] = described("The dates the task has")
    new_dates: list[str] = described(
        "The dates with the date added, if it was not there",
    )
    dates_text: str = described("The new dates in one line: 2026-11-17, 2026-11-24")
    total_cost: float = described(
        "What all the new dates cost together. A whole number if it comes "
        "out as one",
    )


class ReadTally(Action[ReadTallyInput, ReadTallyOutput]):
    """Read the dates that are kept on a task, and what one more comes to.

    Nothing is written. "Write the dates to a task" does that, with the
    values this gives.
    """

    id = "todoist.read-tally"
    title = "Get the dates on a task"

    async def run(self, ctx: StepContext, inputs: ReadTallyInput) -> ReadTallyOutput:
        """Return the dates of the task with the label, and the totals."""
        todoist = await Todoist.for_user(ctx.user)
        task = await _tally_task(todoist, inputs.label)
        dates: list[str] = []
        if task is not None:
            comment = await todoist.get_first_comment_with_yaml(task, create=True)
            dates, _ = _tally_of(comment.content)
        has_date = inputs.date in dates
        new_dates = dates if has_date else [*dates, inputs.date]
        total = inputs.cost * len(new_dates)
        return ReadTallyOutput(
            found=task is not None,
            has_date=has_date,
            dates=dates,
            new_dates=new_dates,
            dates_text=", ".join(new_dates),
            total_cost=int(total) if float(total).is_integer() else total,
        )


@dataclass
class WriteTallyInput:
    """Which task, and what to write to it."""

    label: str = field(metadata={HELP_KEY: "A label that only this task has"})
    # The dates to keep on the task: ``new_dates`` of "Get the dates on a task".
    dates: list[str]
    # What the task is called from now on.
    name: str
    project: str | None = field(
        default=None,
        metadata={HELP_KEY: "The id of the project of the task. Empty: the inbox"},
    )


@dataclass
class WriteTallyOutput:
    """What was written."""

    created: bool = described("Whether the task was made just now")
    count: int = described("How many dates the task has now")


class WriteTally(Action[WriteTallyInput, WriteTallyOutput]):
    """Write dates to the task with a label, name it, and make it due today.

    A task that is not there is made. The dates go into a comment of the
    task, as YAML between two lines of "---". Text around them in the
    comment stays.
    """

    id = "todoist.write-tally"
    title = "Write the dates to a task"

    async def run(self, ctx: StepContext, inputs: WriteTallyInput) -> WriteTallyOutput:
        """Write the dates and the name, and set the due date."""
        todoist = await Todoist.for_user(ctx.user)
        task = await _tally_task(todoist, inputs.label)
        if task is None:
            task = await todoist.add_task(
                content=inputs.name,
                labels=[inputs.label],
                project_id=inputs.project,
            )
            await todoist.add_comment(
                task=task, content=_comment_text("", {"dates": inputs.dates}, "")
            )
            created = True
        else:
            comment = await todoist.get_first_comment_with_yaml(task)
            before, _, after = comment.content.split("---")
            _, held = _tally_of(comment.content)
            held["dates"] = list(inputs.dates)
            await todoist.update_task(
                task, content=inputs.name, project_id=inputs.project
            )
            await todoist.update_comment(
                comment, content=_comment_text(before, held, after)
            )
            created = False
        await todoist.set_due_date_to_today(task)
        return WriteTallyOutput(created=created, count=len(inputs.dates))


def _comment_text(before: str, held: dict[str, Any], after: str) -> str:
    """Return a comment with the YAML between two lines of "---".

    The text after the YAML starts on the line after the second "---". A
    line break that is there already is not added to, or the comment would
    grow by an empty line each time it is written.
    """
    return "---\n".join([before, yaml.safe_dump(held), after.removeprefix("\n")])


def todoist_blocks() -> list[Block]:
    """Return the blocks that the Todoist plugin publishes."""
    return [FindTasks(), SetDueDate(), ReadTally(), WriteTally()]
