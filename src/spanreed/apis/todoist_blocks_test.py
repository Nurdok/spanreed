import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.todoist import Task
from spanreed.apis.todoist_blocks import (
    FindTasks,
    FindTasksInput,
    FindTasksOutput,
    ReadTally,
    ReadTallyInput,
    SetDueDate,
    SetDueDateInput,
    SetDueDateOutput,
    WriteTally,
    WriteTallyInput,
    todoist_blocks,
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


# -- a tally of dates on a task ------------------------------------------------


def test_the_todoist_blocks() -> None:
    assert [block.id for block in todoist_blocks()] == [
        "todoist.find-tasks",
        "todoist.set-due-date",
        "todoist.read-tally",
        "todoist.write-tally",
    ]


def _tally(mock_todoist: MagicMock, comment: str | None, tasks: int = 1) -> Any:
    """Set Todoist up with a task whose comment holds dates, or with none."""
    todoist = mock_todoist.for_user.return_value
    task = SimpleNamespace(id="T1")
    todoist.get_tasks_with_label = AsyncMock(
        return_value=[] if comment is None else [task] * tasks
    )
    todoist.get_first_comment_with_yaml = AsyncMock(
        return_value=SimpleNamespace(id="C1", content=comment)
    )
    todoist.add_task = AsyncMock(return_value=SimpleNamespace(id="T-new"))
    for name in ("add_comment", "update_task", "update_comment"):
        setattr(todoist, name, AsyncMock())
    todoist.set_due_date_to_today = AsyncMock()
    return todoist


def _read(**inputs: Any) -> Any:
    given = {"label": "therapy", "date": "2026-11-24", **inputs}
    return asyncio.run(ReadTally().run(_context(), ReadTallyInput(**given)))


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_read_tally_gives_the_dates_and_what_one_more_comes_to(
    mock_todoist: MagicMock,
) -> None:
    _tally(mock_todoist, "---\ndates:\n- '2026-11-10'\n- '2026-11-17'\n---\n")

    output = _read(cost=150)

    assert (output.found, output.has_date) == (True, False)
    assert output.dates == ["2026-11-10", "2026-11-17"]
    assert output.new_dates == ["2026-11-10", "2026-11-17", "2026-11-24"]
    assert output.dates_text == "2026-11-10, 2026-11-17, 2026-11-24"
    # A whole number when it comes out as one, as the plugin wrote it.
    assert output.total_cost == 450 and isinstance(output.total_cost, int)
    assert _read(cost=12.25).total_cost == 36.75
    assert _read().total_cost == 0


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_read_tally_knows_a_date_that_is_there_and_a_task_that_is_not(
    mock_todoist: MagicMock,
) -> None:
    _tally(mock_todoist, "---\ndates:\n- '2026-11-24'\n---\n")
    there = _read(cost=10)
    assert (there.has_date, there.new_dates, there.total_cost) == (
        True,
        ["2026-11-24"],
        10,
    )

    todoist = _tally(mock_todoist, None)
    none = _read(cost=10)
    assert (none.found, none.has_date, none.dates, none.new_dates) == (
        False,
        False,
        [],
        ["2026-11-24"],
    )
    todoist.get_first_comment_with_yaml.assert_not_awaited()

    # A comment that was just made for the dates is empty.
    _tally(mock_todoist, "---\n---")
    assert _read().dates == []


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_the_tally_needs_one_task_with_the_label(mock_todoist: MagicMock) -> None:
    _tally(mock_todoist, "---\ndates: []\n---\n", tasks=2)

    with pytest.raises(RuntimeError, match="exactly one task with the label therapy"):
        _read()
    with pytest.raises(RuntimeError, match="got 2"):
        asyncio.run(
            WriteTally().run(
                _context(), WriteTallyInput(label="therapy", dates=[], name="x")
            )
        )


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_write_tally_makes_the_task_if_it_is_not_there(mock_todoist: MagicMock) -> None:
    todoist = _tally(mock_todoist, None)

    output = asyncio.run(
        WriteTally().run(
            _context(),
            WriteTallyInput(
                label="therapy", dates=["2026-11-24"], name="Pay $150", project="P1"
            ),
        )
    )

    assert (output.created, output.count) == (True, 1)
    todoist.add_task.assert_awaited_once_with(
        content="Pay $150", labels=["therapy"], project_id="P1"
    )
    made = todoist.add_task.return_value
    todoist.add_comment.assert_awaited_once_with(
        task=made, content="---\ndates:\n- '2026-11-24'\n---\n"
    )
    todoist.set_due_date_to_today.assert_awaited_once_with(made)


@patch("spanreed.apis.todoist_blocks.Todoist", autospec=True)
def test_write_tally_keeps_what_else_the_comment_holds(mock_todoist: MagicMock) -> None:
    todoist = _tally(
        mock_todoist,
        "Keep this.\n---\ndates:\n- '2026-11-17'\nrate: 150\n---\nAnd this.",
    )

    output = asyncio.run(
        WriteTally().run(
            _context(),
            WriteTallyInput(
                label="therapy", dates=["2026-11-17", "2026-11-24"], name="Pay $300"
            ),
        )
    )

    assert output.created is False
    (task,) = todoist.update_task.await_args.args
    assert todoist.update_task.await_args.kwargs == {
        "content": "Pay $300",
        "project_id": None,
    }
    assert todoist.update_comment.await_args.kwargs == {
        "content": "Keep this.\n---\ndates:\n- '2026-11-17'\n- '2026-11-24'\nrate: 150\n---\nAnd this."
    }
    todoist.set_due_date_to_today.assert_awaited_once_with(task)
    todoist.add_task.assert_not_awaited()
