import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.claude import Answer, ClaudePlugin
from spanreed.apis.todoist import Task
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.apis.audible import AudiblePlugin
from spanreed.apis.google_books import GoogleBooksPlugin
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.plugins.health_connect import HealthConnectPlugin
from spanreed.plugins.hevy import HevyPlugin
from spanreed.plugins.timekiller import TimekillerPlugin
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.plugins.withings import WithingsPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import (
    async_return_true,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step
from spanreed.workflows.recipes import Recipe, all_recipes
from spanreed.workflows.testing import fake_storage


def setup_function() -> None:
    commands.reset_registry_for_tests()


def _real_registry() -> BlockRegistry:
    """Return the blocks of the real plugins that publish any."""
    Plugin.reset_registry()
    return BlockRegistry.from_plugins(
        [
            WorkflowsPlugin(),
            TodoistPlugin(),
            ObsidianPlugin(),
            HealthConnectPlugin(),
            WithingsPlugin(),
            HevyPlugin(),
            GoogleBooksPlugin(),
            AudiblePlugin(),
            TimekillerPlugin(),
            ClaudePlugin(),
        ]
    )


def _blocks_of(steps: list[Step]) -> set[str]:
    """Return the blocks of some steps and of the steps inside them."""
    blocks = set()
    for step in steps:
        blocks |= {step.block} | _blocks_of(step.steps)
    return blocks


@pytest.mark.parametrize("recipe", all_recipes().values(), ids=lambda r: r.id)
def test_every_recipe_fits_the_blocks(recipe: Recipe) -> None:
    registry = _real_registry()
    workflow = recipe.workflow_for(4)

    assert WorkflowEngine(registry).problems(workflow) == []
    # The plugins it says it needs are the ones whose blocks it uses.
    used = {workflow.trigger.block, *_blocks_of(workflow.steps)}
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


def test_changing_a_workflow_does_not_change_its_recipe() -> None:
    recipe = all_recipes()["todoist-no-overdue"]
    workflow = recipe.workflow_for(4)

    workflow.steps[0].inputs["label"] = "something-else"
    workflow.trigger.params["every"] = 1

    fresh = recipe.workflow_for(7)
    assert fresh.steps[0].inputs["label"] == "spanreed/no-overdue"
    assert fresh.trigger.params["every"] == 4


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
        "spanreed.workflows.blocks.UserInteractionApi", new=mock_bot
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
        "Updated 2 overdue tasks to today.", parse_html=False, parse_markdown=False
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_no_overdue_is_silent_when_nothing_is_overdue(mock_bot: AsyncMock) -> None:
    todoist, (run,) = _run_no_overdue(mock_bot, [])

    todoist.set_due_date_to_today.assert_not_awaited()
    assert run["status"] == "stopped"
    assert [step["id"] for step in run["steps"]] == ["find", "any"]
    # Only the line that a run by hand ends with if it said nothing.
    mock_bot.send_message.assert_awaited_once_with(
        "Daily chores never overdue: done. The run ended at step 2, Only continue if.",
        parse_html=False,
    )


def test_the_recipe_that_replaces_a_plugin_keeps_the_command_menu_as_it_was() -> None:
    # Todoist No Overdue had no command, so its recipe ships with the
    # command hidden.
    assert all_recipes()["todoist-no-overdue"].command_hidden


SCAN = "Assets/scans/2026-05-02 Scan.pdf"
SKIPPED_SCAN = "Assets/scans/2026-05-01 Scan.pdf"


def _name_scans(
    mock_bot: AsyncMock, choice: int, typed: str = "", runs: int = 1
) -> tuple[Any, Any, list[dict[str, Any]]]:
    """Run "Name scans" by hand, with what the user chooses and types.

    Return the Obsidian mock, the Claude mock and the traces of the runs.
    """
    registry = _real_registry()
    mock_bot.request_user_choice.return_value = choice
    mock_bot.request_user_input.return_value = typed
    claude = MagicMock()
    claude.ask = AsyncMock(
        return_value=Answer(
            data={"name": "Water/gas bill", "date": "2026-04-28"},
            model="claude-opus-5-5",
        )
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = WorkflowEngine(registry)
        user = mock_user_find_by_id(4)
        workflow = await engine.install(user, all_recipes()["obsidian-name-scans"])
        for _ in range(runs):
            await engine.run_by_hand(workflow.id, user)
        found = await engine.store.recent_runs(workflow.id, 10)
        await engine.stop()
        return list(reversed(found))

    with fake_storage(), patch(
        "spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True
    ) as obsidian, patch(
        "spanreed.apis.claude_blocks.ClaudeApi.for_user", AsyncMock(return_value=claude)
    ), patch.object(
        Plugin, "is_registered", async_return_true
    ), patch(
        "spanreed.workflows.blocks.UserInteractionApi", new=mock_bot
    ):
        obsidian.for_user = AsyncMock(return_value=obsidian)
        obsidian.list_dir = AsyncMock(
            return_value=[SKIPPED_SCAN, SCAN, "Assets/scans/2026-04-01 Lease.pdf"]
        )
        obsidian.read_binary_file = AsyncMock(return_value=b"%PDF-1.7")
        obsidian.move_file = AsyncMock()
        obsidian.delete_file = AsyncMock()
        return obsidian, claude, asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_name_scans_gives_a_scan_the_name_that_claude_suggests(
    mock_bot: AsyncMock,
) -> None:
    obsidian, claude, (run,) = _name_scans(mock_bot, choice=0)

    assert run["status"] == "finished", run
    obsidian.read_binary_file.assert_awaited_once_with(SKIPPED_SCAN)
    mock_bot.send_document.assert_awaited_once_with("2026-05-01 Scan.pdf", b"%PDF-1.7")
    (file,) = claude.ask.await_args.kwargs["files"]
    assert (file.name, file.type) == ("2026-05-01 Scan.pdf", "application/pdf")
    assert mock_bot.request_user_choice.await_args.args[1] == [
        "Name it “2026-04-28 Water-gas bill”",
        "Another name",
        "Delete",
        "Skip for good",
        "Not now",
    ]
    obsidian.move_file.assert_awaited_once_with(
        SKIPPED_SCAN, "Assets/scans/2026-04-28 Water-gas bill.pdf"
    )
    obsidian.delete_file.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
def test_name_scans_takes_another_name(mock_bot: AsyncMock) -> None:
    obsidian, _, (run,) = _name_scans(mock_bot, choice=1, typed="Gas bill")

    assert run["status"] == "finished", run
    obsidian.move_file.assert_awaited_once_with(
        SKIPPED_SCAN, "Assets/scans/2026-04-28 Gas bill.pdf"
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_name_scans_deletes_a_scan(mock_bot: AsyncMock) -> None:
    obsidian, _, (run,) = _name_scans(mock_bot, choice=2)

    assert run["status"] == "finished", run
    obsidian.delete_file.assert_awaited_once_with(SKIPPED_SCAN)
    obsidian.move_file.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
def test_name_scans_does_not_ask_again_about_a_scan_that_was_skipped(
    mock_bot: AsyncMock,
) -> None:
    obsidian, _, runs = _name_scans(mock_bot, choice=3, runs=3)

    assert [run["status"] for run in runs] == ["finished", "finished", "stopped"]
    assert [c.args[0] for c in obsidian.read_binary_file.await_args_list] == [
        SKIPPED_SCAN,
        SCAN,
    ]
    obsidian.move_file.assert_not_awaited()
    obsidian.delete_file.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
def test_name_scans_leaves_a_scan_for_later(mock_bot: AsyncMock) -> None:
    obsidian, _, runs = _name_scans(mock_bot, choice=4, runs=2)

    assert [c.args[0] for c in obsidian.read_binary_file.await_args_list] == [
        SKIPPED_SCAN,
        SKIPPED_SCAN,
    ]
    obsidian.move_file.assert_not_awaited()
    obsidian.delete_file.assert_not_awaited()
