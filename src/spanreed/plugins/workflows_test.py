import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import (
    async_return_false,
    async_return_true,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows import migrations
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.recipes import all_recipes
from spanreed.workflows.store import WorkflowStore
from spanreed.workflows.testing import fake_storage

NO_OVERDUE = "Daily chores never overdue"
# The schedule of a new workflow fires at once. With a last event far in the
# future it waits, so that only what a test starts by hand runs.
NOT_DUE = patch(
    "spanreed.workflows.store.StoredTriggerState.get",
    new=AsyncMock(return_value="2999-01-01T00:00:00+00:00"),
)


def setup_function() -> None:
    commands.reset_registry_for_tests()


def _plugin() -> WorkflowsPlugin:
    """Return the plugin with an engine, as after ``run`` started it."""
    Plugin.reset_registry()
    plugin = WorkflowsPlugin()
    registry = BlockRegistry.from_plugins([plugin, TodoistPlugin()])
    plugin._engine = WorkflowEngine(registry)
    return plugin


def test_name() -> None:
    Plugin.reset_registry()
    plugin = WorkflowsPlugin()

    assert plugin.name() == "Workflows"
    assert plugin.canonical_name() == "workflows"
    assert [block.id for block in plugin.blocks()] == [
        "core.schedule",
        "core.at-time",
        "core.random-schedule",
        "core.continue-if",
        "core.if",
        "core.for-each",
        "core.send-message",
        "core.notify",
        "core.command",
        "core.ask-choice",
        "core.ask-each",
        "core.ask-text",
        "core.ask-list",
        "core.stop",
        "core.called",
        "core.call-workflow",
        "core.run-one",
        "core.remember",
        "core.recall",
        "core.forget",
        "core.download-file",
        "core.send-file",
        "core.started",
        "core.error",
    ]


@patch_user_interaction("spanreed.plugins.workflows")
def test_run_starts_the_engine_with_the_stored_workflows(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = WorkflowsPlugin()
    TodoistPlugin()
    user = mock_user_find_by_id(4)

    async def scenario() -> None:
        # A workflow from before the restart.
        stored = all_recipes()["todoist-no-overdue"].workflow_for(4)
        await WorkflowStore().save(stored)

        task = asyncio.create_task(plugin.run())
        while not passing_on.is_set():
            await asyncio.sleep(0)
        engine = plugin.engine
        assert list(engine._sources) == [stored.id]
        retire.assert_awaited_once_with(engine)

        # Stopping the plugin stops the engine.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not engine._sources

    order: list[str] = []
    passing_on = asyncio.Event()

    async def pass_errors_on(users: Any) -> None:
        order.append("passes errors on")
        assert await users() == [user]
        passing_on.set()
        await asyncio.Event().wait()

    with fake_storage(), NOT_DUE, patch.object(
        WorkflowsPlugin, "_register_existing_users", AsyncMock()
    ) as register, patch.object(
        WorkflowsPlugin, "get_users", AsyncMock(return_value=[user])
    ), patch(
        "spanreed.plugins.workflows.migrations.retire_plugins", new=AsyncMock()
    ) as retire, patch(
        "spanreed.plugins.workflows.migrations.convert_plugins",
        new=AsyncMock(side_effect=lambda engine: order.append("converted")),
    ) as convert, patch(
        "spanreed.plugins.workflows.system.say_started",
        new=AsyncMock(side_effect=lambda users: order.append("said it started")),
    ) as say_started, patch(
        "spanreed.plugins.workflows.system.pass_errors_on", new=pass_errors_on
    ):
        asyncio.run(scenario())

    convert.assert_awaited_once()
    # After the plugins were moved, so that what came from one runs too.
    say_started.assert_awaited_once_with([user])
    assert order == ["converted", "said it started", "passes errors on"]

    register.assert_awaited_once()
    mock_bot.register_command.assert_awaited_once()
    assert mock_bot.register_command.call_args.args[1].text == "Manage workflows"


def test_the_engine_runs_also_if_the_start_cannot_be_said() -> None:
    Plugin.reset_registry()
    plugin = WorkflowsPlugin()

    async def scenario() -> None:
        with patch(
            "spanreed.plugins.workflows.system.say_started",
            new=AsyncMock(side_effect=ConnectionError("Redis is away")),
        ), patch.object(WorkflowsPlugin, "get_users", AsyncMock(return_value=[])):
            await plugin._say_started()

    asyncio.run(scenario())


def test_the_engine_is_not_there_before_the_plugin_runs() -> None:
    Plugin.reset_registry()
    plugin = WorkflowsPlugin()

    with pytest.raises(RuntimeError):
        plugin.engine


# -- moving the users of a retired plugin ------------------------------------


@patch_user_interaction("spanreed.workflows.migrations")
def test_users_of_a_retired_plugin_get_its_recipe_once(mock_bot: AsyncMock) -> None:
    plugin = _plugin()

    async def scenario(redis: Any) -> None:
        await redis.sadd("config:plugin:name=todoist-no-overdue:users", 4)
        await redis.sadd("user:4:plugins", "todoist", "todoist-no-overdue")

        await migrations.retire_plugins(plugin.engine)

        (workflow,) = await plugin.engine.store.list_for_user(4)
        assert workflow.name == NO_OVERDUE
        assert workflow.recipe == "todoist-no-overdue"
        assert workflow.enabled
        # The user is no longer registered to the plugin, only to Todoist.
        assert await redis.smembers("user:4:plugins") == {"todoist"}
        assert not await redis.smembers("config:plugin:name=todoist-no-overdue:users")
        mock_bot.notify.assert_awaited_once()
        assert "“Todoist No Overdue” is now a workflow" in (
            mock_bot.notify.call_args.args[0]
        )

        # Doing it again changes nothing.
        await migrations.retire_plugins(plugin.engine)
        assert len(await plugin.engine.store.list_for_user(4)) == 1
        mock_bot.notify.assert_awaited_once()
        await plugin.engine.stop()

    with fake_storage() as redis, NOT_DUE, patch(
        "spanreed.workflows.migrations.redis_api", new=redis
    ), patch(
        "spanreed.workflows.migrations.User.find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        asyncio.run(scenario(redis))


@patch_user_interaction("spanreed.workflows.migrations")
def test_a_move_that_stopped_halfway_does_not_install_a_second_workflow(
    mock_bot: AsyncMock,
) -> None:
    plugin = _plugin()

    async def scenario(redis: Any) -> None:
        # The workflow is there, but the registration was not dropped yet.
        user = mock_user_find_by_id(4)
        await plugin.engine.install(user, all_recipes()["todoist-no-overdue"])
        await redis.sadd("config:plugin:name=todoist-no-overdue:users", 4)

        await migrations.retire_plugins(plugin.engine)

        assert len(await plugin.engine.store.list_for_user(4)) == 1
        assert not await redis.smembers("config:plugin:name=todoist-no-overdue:users")
        mock_bot.notify.assert_not_awaited()
        await plugin.engine.stop()

    with fake_storage() as redis, NOT_DUE, patch(
        "spanreed.workflows.migrations.redis_api", new=redis
    ), patch(
        "spanreed.workflows.migrations.User.find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        asyncio.run(scenario(redis))


@patch_user_interaction("spanreed.workflows.migrations")
def test_a_user_that_cannot_be_moved_does_not_stop_the_others(
    mock_bot: AsyncMock,
) -> None:
    plugin = _plugin()

    async def find_by_id(user_id: int) -> Any:
        if user_id == 9:
            raise AttributeError("a user that is half deleted")
        return mock_user_find_by_id(user_id)

    async def scenario(redis: Any) -> None:
        await redis.sadd("config:plugin:name=todoist-no-overdue:users", 9, 4)

        await migrations.retire_plugins(plugin.engine)

        assert len(await plugin.engine.store.list_for_user(4)) == 1
        # Still registered, so that the next start tries again.
        assert await redis.smembers("config:plugin:name=todoist-no-overdue:users") == {
            "9"
        }
        await plugin.engine.stop()

    with fake_storage() as redis, NOT_DUE, patch(
        "spanreed.workflows.migrations.redis_api", new=redis
    ), patch(
        "spanreed.workflows.migrations.User.find_by_id",
        new=AsyncMock(side_effect=find_by_id),
    ):
        asyncio.run(scenario(redis))


# -- "Manage workflows" ------------------------------------------------------


def _choose(mock_bot: AsyncMock, *labels: str) -> list[tuple[str, list[str]]]:
    """Answer each question by the label of a choice, in order.

    Return a list that fills up with the questions that were asked.
    """
    answers = list(labels)
    asked: list[tuple[str, list[str]]] = []

    async def choose(prompt: str, choices: list[str]) -> int:
        asked.append((prompt, choices))
        wanted = answers.pop(0)
        for index, choice in enumerate(choices):
            if choice.endswith(wanted):
                return index
        raise AssertionError(f"No choice {wanted!r} in {choices!r}")

    mock_bot.request_user_choice = AsyncMock(side_effect=choose)
    return asked


def _manage(plugin: WorkflowsPlugin, installed: bool = True) -> Any:
    """Run "Manage workflows" for user 4, and return their workflows after."""

    async def scenario() -> Any:
        user = mock_user_find_by_id(4)
        if installed:
            await plugin.engine.install(user, all_recipes()["todoist-no-overdue"])
        await plugin._manage(user)
        workflows = await plugin.engine.store.list_for_user(4)
        await plugin.engine.stop()
        return workflows

    with fake_storage(), NOT_DUE:
        return asyncio.run(scenario())


@patch_user_interaction("spanreed.plugins.workflows")
def test_manage_lists_the_workflows_and_turns_one_off(mock_bot: AsyncMock) -> None:
    plugin = _plugin()
    asked = _choose(mock_bot, NO_OVERDUE, "Turn off", "Back", "Done")

    (workflow,) = _manage(plugin)

    assert not workflow.enabled
    assert asked[0] == (
        "Choose a workflow.",
        [f"✅ {NO_OVERDUE}", "Add from a recipe", "Done"],
    )
    status, actions = asked[1]
    assert status == f"“{NO_OVERDUE}” is on.\nIt has not run yet."
    assert actions == [
        "Turn off",
        "Run now",
        "Show in the command menu",
        "Show recent runs",
        "Delete",
        "Back",
    ]
    # Asked again after the change, with the new state.
    assert asked[2][0].startswith(f"“{NO_OVERDUE}” is off.")
    assert asked[2][1][0] == "Turn on"
    assert asked[3][1][0] == f"⬜ {NO_OVERDUE}"


@patch_user_interaction("spanreed.plugins.workflows")
def test_manage_shows_and_hides_the_command(mock_bot: AsyncMock) -> None:
    plugin = _plugin()
    _choose(mock_bot, NO_OVERDUE, "Show in the command menu", "Back", "Done")

    (workflow,) = _manage(plugin)

    assert not workflow.command_hidden


@patch_user_interaction("spanreed.plugins.workflows")
@patch_user_interaction("spanreed.workflows.engine")
def test_manage_runs_a_workflow_and_shows_its_runs(
    mock_engine_bot: AsyncMock, mock_bot: AsyncMock
) -> None:
    plugin = _plugin()
    asked = _choose(mock_bot, NO_OVERDUE, "Run now", "Show recent runs", "Back", "Done")

    with patch("spanreed.apis.todoist_blocks.Todoist", autospec=True) as todoist, patch(
        "spanreed.workflows.blocks.UserInteractionApi", new=mock_engine_bot
    ), patch.object(Plugin, "is_registered", async_return_true):
        todoist.for_user.return_value.get_overdue_tasks_with_label = AsyncMock(
            return_value=[]
        )
        _manage(plugin)

    mock_engine_bot.send_message.assert_awaited_once_with(
        f"{NO_OVERDUE}: done. The run ended at step 2, Only continue if.",
        parse_html=False,
    )
    # The status now has the run, and so does the list of runs.
    assert "Last run, " in asked[2][0]
    assert asked[2][0].endswith("stopped at step 2, Only continue if.")
    runs_message = mock_bot.send_message.call_args.args[0]
    assert runs_message.startswith(f"The last runs of “{NO_OVERDUE}”:\n")
    assert runs_message.endswith("by hand, stopped at step 2, Only continue if.")


@patch_user_interaction("spanreed.plugins.workflows")
def test_manage_deletes_a_workflow_after_asking(mock_bot: AsyncMock) -> None:
    plugin = _plugin()
    asked = _choose(mock_bot, NO_OVERDUE, "Delete", "Delete", "Done")

    assert _manage(plugin) == []

    assert asked[2] == (
        f"Delete “{NO_OVERDUE}”? Its run history is deleted too.",
        ["Delete", "Cancel"],
    )
    assert asked[3] == (
        "You have no workflows yet.",
        ["Add from a recipe", "Done"],
    )


@patch_user_interaction("spanreed.plugins.workflows")
def test_manage_adds_a_workflow_from_a_recipe(mock_bot: AsyncMock) -> None:
    plugin = _plugin()
    asked = _choose(
        mock_bot, "Add from a recipe", NO_OVERDUE, "Add from a recipe", "Cancel", "Done"
    )

    with patch.object(Plugin, "is_registered", async_return_true):
        (workflow,) = _manage(plugin, installed=False)

    assert workflow.name == NO_OVERDUE
    assert workflow.enabled
    (message,) = [call.args[0] for call in mock_bot.send_message.await_args_list]
    assert message.startswith(f"Added “{NO_OVERDUE}”, and it is on.")
    # It was offered the first time, and is not offered a second time.
    assert NO_OVERDUE in asked[1][1]
    assert NO_OVERDUE not in asked[3][1]


@patch_user_interaction("spanreed.plugins.workflows")
def test_manage_does_not_add_a_recipe_whose_plugin_is_missing(
    mock_bot: AsyncMock,
) -> None:
    plugin = _plugin()
    _choose(mock_bot, "Add from a recipe", NO_OVERDUE, "Done")

    with patch.object(Plugin, "is_registered", async_return_false):
        assert _manage(plugin, installed=False) == []

    assert mock_bot.send_message.call_args.args[0].startswith(
        f"“{NO_OVERDUE}” needs Todoist first."
    )
