import asyncio
from types import SimpleNamespace
from unittest.mock import create_autospec

from todoist_api_python.api_async import TodoistAPIAsync

from spanreed.apis.todoist import Todoist, UserConfig


def _todoist() -> Todoist:
    """A Todoist wrapper whose underlying client is an autospec mock.

    Autospec enforces the real v3 method signatures, so a call the wrapper
    makes with the wrong arguments fails the test.
    """
    td = Todoist(UserConfig(api_token="test-token"))
    td._api = create_autospec(TodoistAPIAsync, instance=True)
    return td


def _pages(*pages: list) -> object:
    """An async iterator of pages, matching the v3 paginated endpoints."""

    async def gen() -> object:
        for page in pages:
            yield page

    return gen()


def _task(
    task_id: str = "1", content: str = "c", due: object = None
) -> object:
    return SimpleNamespace(id=task_id, content=content, due=due)


def test_get_overdue_tasks_with_label_flattens_filter() -> None:
    td = _todoist()
    t1, t2, t3 = _task("1"), _task("2"), _task("3")
    td._api.filter_tasks.return_value = _pages([t1, t2], [t3])

    result = asyncio.run(td.get_overdue_tasks_with_label("bills"))

    assert result == [t1, t2, t3]
    td._api.filter_tasks.assert_awaited_once_with(query="@bills & overdue")


def test_get_tasks_with_label_uses_filter_tasks() -> None:
    td = _todoist()
    td._api.filter_tasks.return_value = _pages([])

    asyncio.run(td.get_tasks_with_label("bills"))

    td._api.filter_tasks.assert_awaited_once_with(query="@bills")


def test_get_due_tasks_uses_filter_tasks() -> None:
    td = _todoist()
    td._api.filter_tasks.return_value = _pages([])

    asyncio.run(td.get_due_tasks())

    td._api.filter_tasks.assert_awaited_once_with(
        query="overdue | (today & no time) | due before:+0 hours"
    )


def test_get_projects_flattens_pages() -> None:
    td = _todoist()
    p1 = SimpleNamespace(id="a", is_inbox_project=False)
    p2 = SimpleNamespace(id="b", is_inbox_project=True)
    td._api.get_projects.return_value = _pages([p1], [p2])

    assert asyncio.run(td.get_projects()) == [p1, p2]


def test_get_inbox_tasks_finds_inbox_then_lists() -> None:
    td = _todoist()
    inbox = SimpleNamespace(id="inbox", is_inbox_project=True)
    other = SimpleNamespace(id="other", is_inbox_project=False)
    td._api.get_projects.return_value = _pages([other, inbox])
    task = _task("1")
    td._api.get_tasks.return_value = _pages([task])

    result = asyncio.run(td.get_inbox_tasks())

    assert result == [task]
    td._api.get_tasks.assert_awaited_once_with(project_id="inbox")


def test_add_comment_passes_task_id_and_content() -> None:
    td = _todoist()
    td._api.add_comment.return_value = SimpleNamespace(id="c1", content="x")

    asyncio.run(td.add_comment(_task("42"), content="hello"))

    td._api.add_comment.assert_awaited_once_with(task_id="42", content="hello")


def test_update_task_moves_when_project_id_given() -> None:
    td = _todoist()
    task = _task("7")
    td._api.move_task.return_value = True
    td._api.update_task.return_value = task

    asyncio.run(td.update_task(task, content="new", project_id="proj9"))

    td._api.move_task.assert_awaited_once_with("7", project_id="proj9")
    td._api.update_task.assert_awaited_once_with("7", content="new")


def test_update_task_no_move_without_project_id() -> None:
    td = _todoist()
    task = _task("7")
    td._api.update_task.return_value = task

    asyncio.run(td.update_task(task, content="new"))

    td._api.move_task.assert_not_awaited()
    td._api.update_task.assert_awaited_once_with("7", content="new")


def test_get_first_comment_with_yaml_finds_yaml_comment() -> None:
    td = _todoist()
    plain = SimpleNamespace(id="a", content="just text")
    yaml_comment = SimpleNamespace(id="b", content="---\nkey: v\n---")
    td._api.get_comments.return_value = _pages([plain, yaml_comment])

    result = asyncio.run(td.get_first_comment_with_yaml(_task("1")))

    assert result is yaml_comment
    td._api.get_comments.assert_awaited_once_with(task_id="1")


def test_set_due_date_to_today_recurring_keeps_recurrence() -> None:
    td = _todoist()
    task = _task(
        "5", due=SimpleNamespace(is_recurring=True, string="every week")
    )
    td._api.update_task.return_value = task

    asyncio.run(td.set_due_date_to_today(task))

    td._api.update_task.assert_awaited_once_with("5", due_string="every week")


def test_set_due_date_to_today_non_recurring_sets_today() -> None:
    td = _todoist()
    task = _task("5", due=None)
    td._api.update_task.return_value = task

    asyncio.run(td.set_due_date_to_today(task))

    td._api.update_task.assert_awaited_once_with("5", due_string="today")
