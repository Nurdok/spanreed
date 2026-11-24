"""Withings from end to end: a poll, the workflow, the daily note.

The plugin, the engine, the trigger and the Obsidian blocks are the real
ones. Redis is in memory, and Withings and Obsidian are mocks.
"""

import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.apis.withings import MeasurementType
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.withings import (
    DAILY_NOTE_RECIPE,
    WithingsPlugin,
    convert_to_workflows,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow

TODAY = datetime.date(2026, 5, 9)
YESTERDAY = datetime.date(2026, 5, 8)
NOTE = "Daily/2026-05-09.md"
CONFIG_KEY = "config:plugin:name=withings:user_id=4"
LOGIN = {
    "userid": "u",
    "access_token": "a",
    "refresh_token": "r",
    "expires_in": "3600",
    "scope": "s",
    "token_type": "Bearer",
}
MORNING = {
    MeasurementType.WEIGHT: 80.5,
    MeasurementType.FAT_PERCENTAGE: 20.1,
    MeasurementType.FAT_MASS: 16.2,
}


class Polls:
    """Lets a test say when the trigger asks Withings again.

    It stands in for the wait between two polls.
    """

    def __init__(self) -> None:
        self._go = asyncio.Event()
        self._waiting = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self._waiting.set()
        await self._go.wait()
        self._go.clear()

    async def over(self) -> None:
        """Wait until a poll and the runs for what it found are over."""
        async with asyncio.timeout(3):
            await self._waiting.wait()

    async def again(self) -> None:
        """Let the trigger poll once more, and wait until that is over."""
        await self.over()
        self._waiting.clear()
        self._go.set()
        await self.over()


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    obsidian: AsyncMock
    # What the workflow and the engine tell the user.
    bot: AsyncMock
    polls: Polls
    user: Any
    # What Withings has, by date.
    measurements: dict[datetime.date, dict[MeasurementType, float]] = field(
        default_factory=dict
    )
    # The value that the vault has for a property of a note, if any.
    vault: dict[tuple[str, str], Any] = field(default_factory=dict)

    async def save_old_config(self, **daily_note_fields: bool) -> None:
        """Save a config as the plugin did while it wrote the daily note."""
        config = {**LOGIN, "daily_note_fields": daily_note_fields}
        await self.redis.set(CONFIG_KEY, json.dumps(config))

    async def convert(self) -> Workflow:
        """Give the user the workflow, as the conversion at startup does."""
        if await self.redis.get(CONFIG_KEY) is None:
            await self.save_old_config()
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

    def properties_set(self) -> list[tuple[Any, ...]]:
        return [c.args for c in self.obsidian.set_value_of_property.await_args_list]

    def said(self) -> list[str]:
        return [c.args[0] for c in self.bot.send_message.await_args_list]


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), WithingsPlugin()]
    )
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    polls = Polls()
    withings = AsyncMock()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch.object(Plugin, "is_registered", async_return_true),
        patch(
            "spanreed.apis.withings_blocks.WithingsApi.for_user",
            new=AsyncMock(return_value=withings),
        ),
        patch("spanreed.apis.withings_blocks._today", new=lambda: TODAY),
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as obsidian,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.builtin.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        patch("spanreed.apis.obsidian_blocks.UserInteractionApi", new=bot),
        # A failed run is tried again without the wait.
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
        # The test says when the next poll happens.
        patch("spanreed.workflows.blocks._sleep", new=polls.sleep),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            obsidian=obsidian,
            bot=bot,
            polls=polls,
            user=mock_user_find_by_id(4),
        )

        async def get_measurements(
            start_date: datetime.date, end_date: datetime.date
        ) -> dict[datetime.date, dict[MeasurementType, float]]:
            return {
                date: dict(values)
                for date, values in world.measurements.items()
                if start_date <= date <= end_date
            }

        async def get_property(note: str, name: str) -> Any:
            return world.vault.get((note, name))

        withings.get_measurements.side_effect = get_measurements
        obsidian.for_user = AsyncMock(return_value=obsidian)
        obsidian.safe_generate_today_note = AsyncMock(return_value=None)
        obsidian.get_property = AsyncMock(side_effect=get_property)
        obsidian.set_value_of_property = AsyncMock(return_value=None)
        bot.for_user = AsyncMock(return_value=bot)
        for name in ("send_message", "notify", "request_user_choice"):
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


# -- from Withings to the daily note -------------------------------------------


def test_new_measurements_end_up_in_the_daily_note_and_the_user_is_told() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = dict(MORNING)

        await world.convert()
        await world.polls.over()

        world.obsidian.safe_generate_today_note.assert_awaited()
        assert world.properties_set() == [
            (NOTE, "weight", 80.5),
            (NOTE, "fat-percentage", 20.1),
            (NOTE, "fat-mass", 16.2),
        ]
        # One message for the day, as the plugin sent in the background too.
        assert world.said() == [
            "Logged weight=80.5, fat-percentage=20.1, fat-mass=16.2 for 2026-05-09."
        ]

    _run(scenario)


def test_the_same_measurements_do_not_run_again_at_the_next_poll() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = dict(MORNING)
        await world.convert()

        await world.polls.again()
        await world.polls.again()

        assert await world.statuses() == ["finished"]

    _run(scenario)


def test_a_value_that_the_note_already_has_is_never_replaced() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = dict(MORNING)
        # The user corrected the weight by hand.
        world.vault[(NOTE, "weight")] = 79.9

        await world.convert()
        await world.polls.over()

        assert world.properties_set() == [
            (NOTE, "fat-percentage", 20.1),
            (NOTE, "fat-mass", 16.2),
        ]
        assert world.said() == [
            "Logged fat-percentage=20.1, fat-mass=16.2 for 2026-05-09."
        ]

    _run(scenario)


def test_a_day_that_is_in_the_note_already_is_written_without_a_word() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = dict(MORNING)
        for name in ("weight", "fat-percentage", "fat-mass"):
            world.vault[(NOTE, name)] = 1

        await world.convert()
        await world.polls.over()

        assert world.properties_set() == []
        assert world.said() == []
        assert await world.statuses() == ["stopped"]

    _run(scenario)


def test_a_measurement_taken_later_in_the_day_is_added() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = dict(MORNING)
        await world.convert()
        await world.polls.over()
        for _, name, value in world.properties_set():
            world.vault[(NOTE, name)] = value

        world.measurements[TODAY][MeasurementType.HEART_PULSE] = 60
        await world.polls.again()

        assert world.properties_set()[3:] == [(NOTE, "heart-pulse", 60)]
        assert world.said()[1:] == ["Logged heart-pulse=60 for 2026-05-09."]

    _run(scenario)


def test_a_measurement_of_yesterday_that_arrives_late_is_written() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        await world.polls.over()
        assert await world.statuses() == []

        world.measurements[YESTERDAY] = {MeasurementType.WEIGHT: 80.9}
        await world.polls.again()

        assert world.properties_set() == [("Daily/2026-05-08.md", "weight", 80.9)]

    _run(scenario)


def test_a_day_with_no_daily_note_is_skipped_and_the_user_is_told_once() -> None:
    async def scenario(world: World) -> None:
        world.measurements[YESTERDAY] = {MeasurementType.WEIGHT: 80.9}
        world.obsidian.get_property.side_effect = FileNotFoundError("file not found")

        await world.convert()
        await world.polls.again()

        assert world.properties_set() == []
        assert world.said() == [
            "There is no note Daily/2026-05-08.md, so nothing was written to it."
        ]
        # The run did not fail, so the day is not tried again.
        assert await world.statuses() == ["stopped"]

    _run(scenario)


def test_a_day_is_written_once_obsidian_is_back() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = {MeasurementType.WEIGHT: 80.5}
        attempts = WorkflowEngine.MAX_ATTEMPTS
        world.obsidian.set_value_of_property.side_effect = [
            RuntimeError("Obsidian did not answer")
        ] * attempts + [None]

        await world.convert()
        await world.polls.over()

        # Every try of the first poll failed. The user is told once.
        assert await world.statuses() == ["failed"] * attempts
        world.bot.notify.assert_awaited_once()
        assert "“Withings → daily note” failed" in world.bot.notify.await_args.args[0]
        assert world.said() == []

        # The next poll finds the day again, and Obsidian is back.
        await world.polls.again()

        assert await world.statuses() == ["failed"] * attempts + ["finished"]
        assert world.said() == ["Logged weight=80.5 for 2026-05-09."]

    _run(scenario)


# -- by hand -------------------------------------------------------------------


def test_the_sync_command_is_the_workflows_command() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        user = mock_user_find_by_id(4)
        user.plugins = ["workflows"]

        assert [command.text for command in commands.commands_for_user(user)] == [
            "Sync Withings data"
        ]

    _run(scenario)


def test_by_hand_the_last_week_is_written_and_summed_up() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        await world.polls.over()
        world.measurements[TODAY] = {MeasurementType.WEIGHT: 80.5}
        world.measurements[datetime.date(2026, 5, 5)] = {MeasurementType.WEIGHT: 81.0}
        world.vault[(NOTE, "weight")] = 80.5
        world.bot.request_user_choice.return_value = 1  # The last week

        await world.engine.run_by_hand(workflow.id, world.user)

        world.bot.request_user_choice.assert_awaited_once_with(
            "Sync Withings data for…", ["Today", "The last week", "Cancel"]
        )
        assert world.properties_set() == [("Daily/2026-05-05.md", "weight", 81.0)]
        assert world.said() == [
            "Logged weight=81.0 for 2026-05-05.",
            "Withings → daily note: 2 runs. 1 finished, 1 ended early, 0 failed.",
        ]

    _run(scenario)


# -- from the plugin's settings to the workflow --------------------------------


def test_the_conversion_keeps_the_properties_that_were_on() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_config(
            weight=True, fat_percentage=True, fat_mass=False, heart_pulse=False
        )

        workflow = await world.convert()

        assert workflow.enabled
        (write,) = [step for step in workflow.steps if step.id == "write"]
        assert write.inputs["properties"] == {
            "weight": "{{ trigger.weight }}",
            "fat-percentage": "{{ trigger.fat_percentage }}",
        }
        # The config keeps the login, and nothing else.
        assert await WithingsPlugin.get_saved_config_data(world.user) == LOGIN

    _run(scenario)


def test_a_config_from_before_the_setting_writes_every_property() -> None:
    async def scenario(world: World) -> None:
        await world.redis.set(CONFIG_KEY, json.dumps(LOGIN))

        workflow = await world.convert()

        (write,) = [step for step in workflow.steps if step.id == "write"]
        assert list(write.inputs["properties"]) == [
            "weight",
            "fat-percentage",
            "fat-mass",
            "heart-pulse",
        ]

    _run(scenario)


def test_a_plugin_that_wrote_nothing_becomes_a_workflow_that_is_off() -> None:
    async def scenario(world: World) -> None:
        world.measurements[TODAY] = dict(MORNING)
        await world.save_old_config(
            weight=False, fat_percentage=False, fat_mass=False, heart_pulse=False
        )

        workflow = await world.convert()
        await asyncio.sleep(0)

        assert not workflow.enabled
        (write,) = [step for step in workflow.steps if step.id == "write"]
        assert len(write.inputs["properties"]) == 4
        assert world.properties_set() == []

    _run(scenario)


def test_the_conversion_done_again_installs_nothing() -> None:
    async def scenario(world: World) -> None:
        await world.convert()

        assert await convert_to_workflows(world.engine, world.user) == []
        assert len(await world.engine.store.list_for_user(4)) == 1

    _run(scenario)


def test_a_user_of_the_plugin_is_converted_once_at_startup_and_told() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_config()
        await world.redis.sadd("config:plugin:name=withings:users", 4)

        await migrations.convert_plugins(world.engine)
        await migrations.convert_plugins(world.engine)

        workflow = await world.workflow()
        assert workflow.recipe == DAILY_NOTE_RECIPE
        world.bot.notify.assert_awaited_once()
        assert world.bot.notify.await_args.args[0] == (
            "Withings now does its work through workflows: “Withings → daily "
            "note”. They do what the plugin did, with your settings. See them "
            "under Workflows in the web UI, or with “Manage workflows”."
        )
        # The user stays a user of the plugin: it still holds the login.
        assert await world.redis.smembers("config:plugin:name=withings:users") == {"4"}

    _run(scenario)
