import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.apis.todoist import Task
from spanreed.apis.todoist_blocks import (
    FindTasks,
    FindTasksInput,
    FindTasksOutput,
    SetDueDate,
    SetDueDateInput,
    SetDueDateOutput,
)
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.blocks import RunInfo, StepContext


def _context() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="r1", manual=False),
    )


def _task(name: str) -> MagicMock:
    return MagicMock(name=name, spec=Task)


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_find_tasks_finds_the_tasks_with_a_label(mock_todoist: MagicMock) -> None:
    tasks = [_task("one"), _task("two")]
    todoist = mock_todoist.for_user.return_value
    todoist.get_tasks_with_label = AsyncMock(return_value=tasks)

    output = asyncio.run(FindTasks().run(_context(), FindTasksInput(label="chores")))

    todoist.get_tasks_with_label.assert_awaited_once_with("chores")
    assert output == FindTasksOutput(tasks=tasks, count=2)


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_find_tasks_can_find_only_the_overdue_ones(mock_todoist: MagicMock) -> None:
    todoist = mock_todoist.for_user.return_value
    todoist.get_overdue_tasks_with_label = AsyncMock(return_value=[])

    output = asyncio.run(
        FindTasks().run(_context(), FindTasksInput(label="chores", due="overdue"))
    )

    todoist.get_overdue_tasks_with_label.assert_awaited_once_with("chores")
    assert output == FindTasksOutput(tasks=[], count=0)


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_set_due_date_moves_every_task_to_today(mock_todoist: MagicMock) -> None:
    tasks = [_task("one"), _task("two")]
    todoist = mock_todoist.for_user.return_value
    todoist.set_due_date_to_today = AsyncMock()

    output = asyncio.run(SetDueDate().run(_context(), SetDueDateInput(tasks=tasks)))

    assert [c.args[0] for c in todoist.set_due_date_to_today.await_args_list] == tasks
    assert output == SetDueDateOutput(count=2)
