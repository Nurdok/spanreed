"""Hevy from end to end: a poll, the workflow, the set notes.

The plugin, the engine, the trigger and the Obsidian blocks are the real
ones. Redis is in memory, and Hevy and Obsidian are mocks.
"""

import asyncio
import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.hevy import Workout
from spanreed.apis.hevy_test_data import EXPECTED_NOTES, WORKOUT
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.hevy import (
    SET_NOTES_RECIPE,
    SINCE_KEY,
    SYNCED_IDS_KEY,
    HevyPlugin,
    convert_to_workflows,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.testing import Polls

CONFIG_KEY = "config:plugin:name=hevy:user_id=4"
LOGGED = 'Logged Hevy workout: Push: day "A" (5 sets across 4 exercises).'


def _workout(workout_id: str = "w-1", start: str = "2026-05-09T17:30:00Z") -> Workout:
    return Workout.from_json({**WORKOUT, "id": workout_id, "start_time": start})


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    hevy: AsyncMock
    obsidian: AsyncMock
    # What the workflow and the engine tell the user.
    bot: AsyncMock
    polls: Polls
    user: Any
    # The workouts that Hevy has, oldest first. A poll is given all of
    # them, as when each was changed since the cursor.
    workouts: list[Workout] = field(default_factory=list)

    async def save_old_state(
        self,
        vault_dir: str = "Fitness/Sets",
        since: str | None = None,
        synced: list[str] | None = None,
    ) -> None:
        """Save what the plugin kept while it wrote the notes itself."""
        config = {"api_key": "key", "vault_dir": vault_dir}
        await self.redis.set(CONFIG_KEY, json.dumps(config))
        if since is not None:
            await HevyPlugin.set_user_data(self.user, SINCE_KEY, since)
        if synced is not None:
            await HevyPlugin.set_user_data(
                self.user, SYNCED_IDS_KEY, json.dumps(synced)
            )

    async def convert(self) -> Workflow:
        """Give the user the workflow, as the conversion at startup does."""
        if await self.redis.get(CONFIG_KEY) is None:
            await self.save_old_state()
        await convert_to_workflows(self.engine, self.user)
        return await self.workflow()

    async def workflow(self) -> Workflow:
        (workflow,) = await self.engine.store.list_for_user(4)
        return workflow

    async def statuses(self) -> list[str]:
        """Return how each run of the workflow ended, oldest first."""
        workflow = await self.workflow()
        runs = await self.engine.store.recent_runs(workflow.id, 100)
        return [run["status"] for run in reversed(runs)]

    def notes_written(self) -> dict[str, str]:
        """Return the text of each note written to the vault, by path."""
        notes = {}
        for call in self.obsidian.write_binary_file.await_args_list:
            assert call.kwargs == {"overwrite": True}
            notes[call.args[0]] = call.args[1].decode("utf-8")
        return notes

    def writes(self) -> int:
        return len(self.obsidian.write_binary_file.await_args_list)

    def polled_since(self) -> list[str]:
        calls = self.hevy.get_updated_workouts_since.await_args_list
        return [call.args[0] for call in calls]

    def said(self) -> list[str]:
        return [c.args[0] for c in self.bot.send_message.await_args_list]


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), HevyPlugin()]
    )
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    polls = Polls()
    hevy = AsyncMock()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.hevy.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch.object(Plugin, "is_registered", async_return_true),
        patch(
            "spanreed.apis.hevy_blocks.HevyApi.for_user",
            new=AsyncMock(return_value=hevy),
        ),
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as obsidian,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.builtin.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        # A failed run is tried again without the wait.
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
        # The test says when the next poll happens.
        patch("spanreed.workflows.blocks._sleep", new=polls.sleep),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            hevy=hevy,
            obsidian=obsidian,
            bot=bot,
            polls=polls,
            user=mock_user_find_by_id(4),
        )

        async def workouts(*_: Any) -> list[Workout]:
            return list(world.workouts)

        async def workout(workout_id: str) -> Workout:
            return next(w for w in world.workouts if w.id == workout_id)

        hevy.get_updated_workouts_since.side_effect = workouts
        hevy.get_all_workouts.side_effect = workouts
        hevy.get_workout.side_effect = workout
        obsidian.for_user = AsyncMock(return_value=obsidian)
        obsidian.write_binary_file = AsyncMock(return_value=None)
        bot.for_user = AsyncMock(return_value=bot)
        for name in (
            "send_message",
            "notify",
            "request_user_choice",
            "request_user_input",
        ):
            setattr(bot, name, AsyncMock())
        yield world


def _run(scenario: Any) -> None:
    """Run a scenario in a new world, and stop the engine after it."""

    async def with_engine(world: World) -> None:
        try:
            await scenario(world)
        finally:
            await world.engine.stop()

    with _world() as world:
        asyncio.run(with_engine(world))


# -- from Hevy to the set notes ------------------------------------------------


def test_a_new_workout_becomes_the_notes_that_the_plugin_wrote() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout()]

        await world.convert()
        await world.polls.over()

        assert world.notes_written() == EXPECTED_NOTES
        # In the order the sets were done.
        assert list(world.notes_written()) == list(EXPECTED_NOTES)
        assert world.said() == [LOGGED]

    _run(scenario)


def test_a_workout_is_written_once_however_often_hevy_returns_it() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout()]
        await world.convert()

        await world.polls.again()
        await world.polls.again()

        assert await world.statuses() == ["finished"]
        assert world.writes() == 5
        # Each poll asks for what changed since the one before it.
        first, second, third = world.polled_since()
        assert first == "1970-01-01T00:00:00Z"
        assert first < second <= third

    _run(scenario)


def test_a_workout_that_could_not_be_written_is_written_by_a_later_try() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout()]
        # Obsidian stops answering at the third set, once.
        world.obsidian.write_binary_file.side_effect = [
            None,
            None,
            RuntimeError("Obsidian did not answer"),
        ] + [None] * 5

        await world.convert()
        await world.polls.over()

        assert await world.statuses() == ["failed", "finished"]
        # The second try wrote every note again, over the first two.
        assert world.writes() == 8
        assert world.notes_written() == EXPECTED_NOTES
        # The workout is announced once, when all of it is written.
        assert world.said() == [LOGGED]

    _run(scenario)


def test_the_cursor_stays_while_a_workout_is_not_written() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout()]
        attempts = WorkflowEngine.MAX_ATTEMPTS
        world.obsidian.write_binary_file.side_effect = [
            RuntimeError("Obsidian did not answer")
        ] * attempts + [None] * 5

        await world.convert()
        await world.polls.over()
        world.bot.notify.reset_mock()

        # Every try of the first poll failed, so the next poll asks Hevy
        # for the same stretch of time, and finds the workout again.
        await world.polls.again()

        assert world.polled_since() == ["1970-01-01T00:00:00Z"] * 2
        assert await world.statuses() == ["failed"] * attempts + ["finished"]
        assert world.said() == [LOGGED]

    _run(scenario)


# -- by hand -------------------------------------------------------------------


def test_the_sync_command_is_the_workflows_command() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        user = mock_user_find_by_id(4)
        user.plugins = ["workflows"]

        assert [command.text for command in commands.commands_for_user(user)] == [
            "Sync Hevy workouts"
        ]

    _run(scenario)


def test_by_hand_new_workouts_are_written_and_not_again_by_the_next_poll() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        await world.polls.over()
        world.workouts = [_workout()]
        world.bot.request_user_choice.return_value = 0  # New workouts

        await world.engine.run_by_hand(workflow.id, world.user)

        world.bot.request_user_choice.assert_awaited_once_with(
            "Write Hevy workouts for…",
            [
                "New workouts",
                "A specific date",
                "A workout ID",
                "All workouts",
                "Cancel",
            ],
        )
        assert world.said() == [LOGGED, "Hevy workouts → set notes: finished."]

        await world.polls.again()

        assert world.writes() == 5
        assert await world.statuses() == ["finished"]

    _run(scenario)


def test_by_hand_a_date_writes_its_workouts_again_without_announcing_them() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout("old", "2026-05-01T17:00:00Z"), _workout()]
        workflow = await world.convert()
        await world.polls.over()
        world.obsidian.write_binary_file.reset_mock()
        world.bot.send_message.reset_mock()
        world.bot.request_user_choice.return_value = 1  # A specific date
        world.bot.request_user_input.return_value = "2026-05-09"

        await world.engine.run_by_hand(workflow.id, world.user)

        world.bot.request_user_input.assert_awaited_once_with(
            "Enter the date (YYYY-MM-DD):"
        )
        assert world.notes_written() == EXPECTED_NOTES
        # The run ends at "Only continue if": the workout is not new, so it
        # is not announced.
        assert world.said() == [
            "Hevy workouts → set notes: done. The run ended at step 2, Only continue if."
        ]

    _run(scenario)


def test_by_hand_all_workouts_asks_first() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout("a"), _workout("b")]
        workflow = await world.convert()
        await world.polls.over()
        world.obsidian.write_binary_file.reset_mock()
        world.bot.send_message.reset_mock()
        # All workouts, then Cancel instead of Yes.
        world.bot.request_user_choice.side_effect = [3, 1]

        await world.engine.run_by_hand(workflow.id, world.user)

        assert world.bot.request_user_choice.await_args_list[1].args == (
            "Write the notes of ALL workouts again?",
            ["Yes", "Cancel"],
        )
        assert world.writes() == 0

        world.bot.request_user_choice.side_effect = [3, 0]
        await world.engine.run_by_hand(workflow.id, world.user)

        assert world.writes() == 10
        assert world.said() == [
            "Hevy workouts → set notes: 2 runs. 0 finished, 2 ended early, 0 failed."
        ]

    _run(scenario)


def test_by_hand_a_date_that_is_no_date_says_so() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        world.bot.request_user_choice.return_value = 1  # A specific date
        world.bot.request_user_input.return_value = "yesterday"

        await world.engine.run_by_hand(workflow.id, world.user)

        assert world.said() == ["That doesn’t look like a YYYY-MM-DD date."]

    _run(scenario)


# -- from what the plugin kept to the workflow ---------------------------------


def test_the_conversion_keeps_the_folder() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout()]
        await world.save_old_state(vault_dir="Gym/Sets")

        workflow = await world.convert()
        await world.polls.over()

        (each,) = [step for step in workflow.steps if step.id == "each"]
        assert each.steps[0].inputs["path"] == (
            "Gym/Sets/{{ trigger.folder }}/{{ each.item.note_name }}.md"
        )
        assert list(world.notes_written()) == [
            path.replace("Fitness/Sets/", "Gym/Sets/") for path in EXPECTED_NOTES
        ]
        # The config keeps the API key, and nothing else.
        assert await HevyPlugin.get_saved_config_data(world.user) == {"api_key": "key"}

    _run(scenario)


def test_the_conversion_goes_on_where_the_plugin_was() -> None:
    async def scenario(world: World) -> None:
        # Hevy returns both: "w-old" was changed after the plugin wrote it.
        world.workouts = [_workout("w-old", "2026-05-08T17:00:00Z"), _workout()]
        await world.save_old_state(since="2026-05-09T12:00:00Z", synced=["w-old"])

        workflow = await world.convert()
        await world.polls.over()

        assert workflow.enabled
        # The first poll asks from where the plugin's last poll was.
        assert world.polled_since() == ["2026-05-09T12:00:00Z"]
        # Only the new workout is written and announced.
        assert world.notes_written() == EXPECTED_NOTES
        assert world.said() == [LOGGED]
        # What the plugin kept is the trigger's now.
        assert await HevyPlugin.get_user_data(world.user, SINCE_KEY) is None
        assert await HevyPlugin.get_user_data(world.user, SYNCED_IDS_KEY) is None

    _run(scenario)


def test_a_conversion_that_stopped_halfway_is_finished_by_the_next() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout("w-old")]
        await world.save_old_state(since="2026-05-09T12:00:00Z", synced=["w-old"])
        # The first time stops after the workflow is stored, turned off.
        with patch(
            "spanreed.plugins.hevy.PollTrigger.start_from",
            new=AsyncMock(side_effect=RuntimeError("Redis went away")),
        ):
            try:
                await convert_to_workflows(world.engine, world.user)
            except RuntimeError:
                pass
        assert not (await world.workflow()).enabled

        assert await convert_to_workflows(world.engine, world.user) == []
        await world.polls.over()

        assert (await world.workflow()).enabled
        assert world.polled_since() == ["2026-05-09T12:00:00Z"]
        assert world.writes() == 0

    _run(scenario)


def test_a_user_who_never_synced_gets_a_workflow_that_starts_at_the_beginning() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        await world.polls.over()

        assert workflow.enabled
        assert world.polled_since() == ["1970-01-01T00:00:00Z"]

    _run(scenario)


def test_the_conversion_done_again_changes_nothing() -> None:
    async def scenario(world: World) -> None:
        world.workouts = [_workout()]
        workflow = await world.convert()
        await world.polls.over()
        workflow.enabled = False
        await world.engine.save(workflow)

        assert await convert_to_workflows(world.engine, world.user) == []

        # A workflow that the user turned off stays off.
        assert not (await world.workflow()).enabled

    _run(scenario)


def test_a_user_of_the_plugin_is_converted_once_at_startup_and_told() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_state()
        await world.redis.sadd("config:plugin:name=hevy:users", 4)

        await migrations.convert_plugins(world.engine)
        await migrations.convert_plugins(world.engine)

        workflow = await world.workflow()
        assert workflow.recipe == SET_NOTES_RECIPE
        world.bot.notify.assert_awaited_once()
        assert world.bot.notify.await_args.args[0] == (
            "Hevy now does its work through workflows: “Hevy workouts → set "
            "notes”. They do what the plugin did, with your settings. See them "
            "under Workflows in the web UI, or with “Manage workflows”. The "
            "commands Sync Hevy workouts and Re-create Hevy data are now one, "
            "“Sync Hevy workouts”, which asks what to write."
        )
        # The user stays a user of the plugin: it still holds the API key.
        assert await world.redis.smembers("config:plugin:name=hevy:users") == {"4"}

    _run(scenario)
