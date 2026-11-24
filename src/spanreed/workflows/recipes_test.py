import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.todoist import Task
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import (
    async_return_true,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.recipes import Recipe, all_recipes
from spanreed.workflows.testing import fake_storage


def setup_function() -> None:
    commands.reset_registry_for_tests()


def _real_registry() -> BlockRegistry:
    """Return the blocks of the real plugins that publish any."""
    Plugin.reset_registry()
    return BlockRegistry.from_plugins([WorkflowsPlugin(), TodoistPlugin()])


@pytest.mark.parametrize("recipe", all_recipes().values(), ids=lambda r: r.id)
def test_every_recipe_fits_the_blocks(recipe: Recipe) -> None:
    registry = _real_registry()
    workflow = recipe.workflow_for(4)

    assert WorkflowEngine(registry).problems(workflow) == []
    # The plugins it says it needs are the ones whose blocks it uses.
    used = {workflow.trigger.block, *(step.block for step in workflow.steps)}
    plugins = {registry.plugin_of(block).canonical_name() for block in used}
    assert set(recipe.requires) == plugins - {"workflows"}


def test_the_plugins_that_spanreed_loads_publish_what_the_recipes_need() -> None:
    from spanreed.__main__ import load_plugins

    Plugin.reset_registry()
    engine = WorkflowEngine(BlockRegistry.from_plugins(load_plugins()))

    for recipe in all_recipes().values():
        assert engine.problems(recipe.workflow_for(4)) == []


def test_a_recipe_gives_each_user_a_workflow_of_their_own() -> None:
    recipe = all_recipes()["todoist-no-overdue"]
    first, second = recipe.workflow_for(4), recipe.workflow_for(7)

    assert first.id != second.id
    assert (first.user_id, second.user_id) == (4, 7)
    assert first.recipe == "todoist-no-overdue"
    assert first.enabled


def _run_no_overdue(mock_bot: AsyncMock, tasks: list[Any]) -> tuple[Any, list[Any]]:
    """Run "Daily chores never overdue" once, by hand.

    Return the Todoist mock and the traces of the runs.
    """
    registry = _real_registry()

    async def scenario() -> list[dict[str, Any]]:
        engine = WorkflowEngine(registry)
        user = mock_user_find_by_id(4)
        workflow = await engine.install(user, all_recipes()["todoist-no-overdue"])
        await engine.run_by_hand(workflow.id, user)
        runs = await engine.store.recent_runs(workflow.id, 10)
        await engine.stop()
        return [run for run in runs if run["manual"]]

    with fake_storage(), patch(
        "spanreed.apis.todoist_blocks.Todoist", autospec=True
    ) as mock_todoist, patch.object(Plugin, "is_registered", async_return_true), patch(
        "spanreed.workflows.builtin.UserInteractionApi", new=mock_bot
    ), patch(
        # The schedule waits for its interval instead of running too.
        "spanreed.workflows.store.StoredTriggerState.get",
        new=AsyncMock(return_value="2999-01-01T00:00:00+00:00"),
    ):
        todoist = mock_todoist.for_user.return_value
        todoist.get_overdue_tasks_with_label = AsyncMock(return_value=tasks)
        todoist.set_due_date_to_today = AsyncMock()
        return todoist, asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_no_overdue_moves_the_overdue_tasks_and_says_so(mock_bot: AsyncMock) -> None:
    tasks = [MagicMock(name="one", spec=Task), MagicMock(name="two", spec=Task)]

    todoist, (run,) = _run_no_overdue(mock_bot, tasks)

    todoist.get_overdue_tasks_with_label.assert_awaited_once_with("spanreed/no-overdue")
    assert [c.args[0] for c in todoist.set_due_date_to_today.await_args_list] == tasks
    assert run["status"] == "finished"
    mock_bot.send_message.assert_any_await(
        "Updated 2 overdue tasks to today.", parse_html=False
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_no_overdue_is_silent_when_nothing_is_overdue(mock_bot: AsyncMock) -> None:
    todoist, (run,) = _run_no_overdue(mock_bot, [])

    todoist.set_due_date_to_today.assert_not_awaited()
    assert run["status"] == "stopped"
    assert [step["id"] for step in run["steps"]] == ["find", "any"]
    # Only the line that every run by hand ends with.
    mock_bot.send_message.assert_awaited_once_with(
        "Daily chores never overdue: stopped at step 2, Only continue if.",
        parse_html=False,
    )


def test_the_recipe_that_replaces_a_plugin_keeps_the_command_menu_as_it_was() -> None:
    # Todoist No Overdue had no command, so its recipe ships with the
    # command hidden.
    assert all_recipes()["todoist-no-overdue"].command_hidden
