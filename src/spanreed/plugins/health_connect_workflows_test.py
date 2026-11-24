"""Health Connect from end to end: a payload, the workflows, the vault.

The plugin, the engine, the triggers and the Obsidian blocks are the real
ones. Redis is in memory and Obsidian is a mock.
"""

import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.health_connect import HealthConnectStore
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.health_connect import (
    DAILY_NOTE_RECIPE,
    RAW_FILES_RECIPE,
    HealthConnectPlugin,
    UserConfig,
    convert_to_workflows,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow

DATE = datetime.date(2026, 5, 9)
NOTE = "Daily/2026-05-09.md"
CONFIG_KEY = "config:plugin:name=health-connect:user_id=4"


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    plugin: HealthConnectPlugin
    engine: WorkflowEngine
    obsidian: AsyncMock
    # What the workflows and the engine tell the user.
    bot: AsyncMock
    user: Any

    async def convert(self) -> tuple[Workflow, Workflow]:
        """Give the user the two workflows, as the conversion at startup does."""
        await convert_to_workflows(self.engine, self.user)
        return await self.workflows()

    async def workflows(self) -> tuple[Workflow, Workflow]:
        """Return the daily note workflow and the raw files workflow."""
        by_recipe = {w.recipe: w for w in await self.engine.store.list_for_user(4)}
        return by_recipe[DAILY_NOTE_RECIPE], by_recipe[RAW_FILES_RECIPE]

    async def receive(self, payload: dict) -> None:
        """Take a payload as the plugin does, and wait for the workflows."""
        summary = await self.plugin._ingest(self.user, json.dumps(payload))
        await self.plugin._announce(self.user, summary)
        await self.settle()

    async def settle(self) -> None:
        """Wait until no workflow has anything left to run for."""
        async with asyncio.timeout(3):
            while True:
                queues = [
                    await self.engine.store.queue(workflow.id).keys()
                    for workflow in await self.engine.store.list_for_user(4)
                ]
                if not any(queues):
                    return
                await asyncio.sleep(0)

    def properties_set(self) -> list[tuple[Any, ...]]:
        return [c.args for c in self.obsidian.set_value_of_property.await_args_list]

    def files_written(self) -> dict[str, str]:
        """Return the text of each file written to the vault, by path."""
        files = {}
        for call in self.obsidian.write_binary_file.await_args_list:
            assert call.kwargs == {"overwrite": True}
            files[call.args[0]] = call.args[1].decode("utf-8")
        return files

    def said(self) -> list[str]:
        return [c.args[0] for c in self.bot.send_message.await_args_list]


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    registry = BlockRegistry.from_plugins([WorkflowsPlugin(), ObsidianPlugin(), plugin])
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.health_connect.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch(
            "spanreed.apis.health_connect_blocks.User.get_user_birth_date",
            new=AsyncMock(return_value=None),
        ),
        patch.object(Plugin, "is_registered", async_return_true),
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as obsidian,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.builtin.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        # A failed run is tried again without the wait.
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
    ):
        obsidian.for_user = AsyncMock(return_value=obsidian)
        for name in (
            "safe_generate_today_note",
            "get_property",
            "set_value_of_property",
            "write_binary_file",
            "delete_file",
        ):
            setattr(obsidian, name, AsyncMock(return_value=None))
        bot.for_user = AsyncMock(return_value=bot)
        for name in ("send_message", "notify", "request_user_choice"):
            setattr(bot, name, AsyncMock())
        yield World(
            redis=fake,
            plugin=plugin,
            engine=WorkflowEngine(registry),
            obsidian=obsidian,
            bot=bot,
            user=mock_user_find_by_id(4),
        )


def _run(scenario: Any) -> None:
    """Run a scenario in a new world, and stop the engine after it."""

    async def with_engine(world: World) -> None:
        try:
            await scenario(world)
        finally:
            await world.engine.stop()

    with _world() as world:
        asyncio.run(with_engine(world))


def _steps_record(record_id: str, hour: int, count: int) -> dict:
    """Return a steps record of DATE as the bridge app sends it."""
    return {
        "count": count,
        "start_time": f"2026-05-09T{hour:02d}:00:00Z",
        "end_time": f"2026-05-09T{hour:02d}:10:00Z",
        "metadata": {"id": record_id},
    }


def _line(record: Any) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


# -- from a payload to the vault -----------------------------------------------


def test_a_payload_ends_up_in_the_daily_note_and_in_the_raw_files() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        first, second = _steps_record("a", 8, 100), _steps_record("b", 9, 250)

        await world.receive(
            {
                "timestamp": "2026-05-09T14:00:00Z",
                "steps": [first, second],
                "resting_heart_rate": [{"bpm": 58, "time": "2026-05-09T06:00:00Z"}],
                "sleep": [{"session_end_time": "2026-05-09T06:00:00Z"}],
            }
        )

        # The day's metrics, in the order of the recipe's properties.
        assert world.properties_set() == [
            (NOTE, "steps", 350),
            (NOTE, "resting-heart-rate", 58),
        ]
        world.obsidian.safe_generate_today_note.assert_awaited()
        # One file for each record type, also for one that has no metric.
        assert world.files_written() == {
            "Health/raw/resting_heart_rate/2026-05-09.jsonl": _line(
                {"bpm": 58, "time": "2026-05-09T06:00:00Z"}
            ),
            "Health/raw/sleep/2026-05-09.jsonl": _line(
                {"session_end_time": "2026-05-09T06:00:00Z"}
            ),
            "Health/raw/steps/2026-05-09.jsonl": _line(first) + _line(second),
        }
        # What the phone sends by itself is written without a word.
        assert world.said() == []

    _run(scenario)


def test_a_value_that_the_note_already_has_is_not_written_again() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        world.obsidian.get_property.side_effect = lambda note, name: (
            350 if name == "steps" else None
        )

        await world.receive(
            {"steps": [_steps_record("a", 8, 100), _steps_record("b", 9, 250)]}
        )

        assert world.properties_set() == []

    _run(scenario)


def test_the_step_total_is_written_again_after_a_deletion() -> None:
    async def scenario(world: World) -> None:
        await world.convert()

        await world.receive(
            {"steps": [_steps_record("a", 8, 100), _steps_record("b", 9, 250)]}
        )
        await world.receive({"deleted": [{"type": "steps", "id": "b"}]})

        totals = [value for _, name, value in world.properties_set() if name == "steps"]
        assert totals == [350, 100]

    _run(scenario)


def test_a_deletion_rewrites_the_raw_files_and_keeps_what_was_deleted() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        kept, gone = _steps_record("keep", 8, 100), _steps_record("gone", 9, 250)
        entries = [{"type": "steps", "id": "gone"}, {"type": "sleep", "id": "unknown"}]
        await world.receive(
            {"timestamp": "2026-05-09T14:00:00Z", "steps": [kept, gone]}
        )
        world.obsidian.write_binary_file.reset_mock()

        await world.receive({"timestamp": "2026-05-10T14:00:00Z", "deleted": entries})

        files = world.files_written()
        assert files["Health/raw/steps/2026-05-09.jsonl"] == _line(kept)
        # All data is kept: the entries as sent, and the record that was removed.
        assert sorted(files["Health/raw/deleted/2026-05-10.jsonl"].splitlines()) == (
            sorted(_line(entry).strip() for entry in entries)
        )
        assert files["Health/raw/deleted_records/2026-05-10.jsonl"] == _line(
            {"type": "steps", "record": gone}
        )
        assert len(files) == 3
        world.obsidian.delete_file.assert_not_awaited()

    _run(scenario)


def test_deleting_the_last_record_of_a_day_deletes_its_raw_file() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        weight = {
            "kilograms": 70.5,
            "time": "2026-05-09T07:00:00Z",
            "metadata": {"id": "w"},
        }
        await world.receive({"weight": [weight]})
        # The first try fails, and the run is tried again.
        world.obsidian.delete_file.side_effect = [TimeoutError("offline"), None]

        await world.receive(
            {
                "timestamp": "2026-05-09T14:00:00Z",
                "deleted": [{"type": "weight", "id": "w"}],
            }
        )

        path = "Health/raw/weight/2026-05-09.jsonl"
        assert [c.args for c in world.obsidian.delete_file.await_args_list] == [
            (path,),
            (path,),
        ]

    _run(scenario)


def test_a_raw_file_that_could_not_be_written_is_written_when_obsidian_is_back() -> (
    None
):
    async def scenario(world: World) -> None:
        _, raw = await world.convert()
        attempts = WorkflowEngine.MAX_ATTEMPTS
        # Obsidian is closed for every try of the first round.
        world.obsidian.write_binary_file.side_effect = [
            *([TimeoutError("offline")] * attempts),
            None,
        ]

        await world.receive(
            {"heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}]}
        )

        # The day stayed in the queue until its file was written.
        assert world.obsidian.write_binary_file.await_count == attempts + 1
        assert await world.engine.store.queue(raw.id).keys() == []
        # The user was told once that the workflow fails.
        world.bot.notify.assert_awaited_once()
        assert (
            "“Health raw data → vault files” failed"
            in world.bot.notify.await_args.args[0]
        )

    _run(scenario)


# -- by hand -------------------------------------------------------------------


def test_sync_health_data_asks_for_a_range_and_says_what_it_logged() -> None:
    async def scenario(world: World) -> None:
        daily, _ = await world.convert()
        await world.receive({"steps": [_steps_record("a", 8, 100)]})
        world.obsidian.set_value_of_property.reset_mock()
        world.bot.request_user_choice.return_value = 2  # Every stored day

        await world.engine.run_by_hand(daily.id, world.user)

        world.bot.request_user_choice.assert_awaited_once_with(
            "Sync health data for…",
            ["Today", "The last week", "Every stored day", "Cancel"],
        )
        assert world.properties_set() == [(NOTE, "steps", 100)]
        assert world.said() == [
            "Logged steps=100 for 2026-05-09.",
            "Health data → daily note: finished.",
        ]

    _run(scenario)


def test_sync_health_data_says_nothing_about_a_day_that_was_up_to_date() -> None:
    async def scenario(world: World) -> None:
        daily, _ = await world.convert()
        await world.receive({"steps": [_steps_record("a", 8, 100)]})
        world.obsidian.get_property.side_effect = lambda note, name: (
            100 if name == "steps" else None
        )
        world.bot.request_user_choice.return_value = 2  # Every stored day

        await world.engine.run_by_hand(daily.id, world.user)

        assert world.said() == [
            "Health data → daily note: stopped at step 3, Only continue if."
        ]

    _run(scenario)


def test_the_export_command_writes_the_file_of_every_stored_day() -> None:
    async def scenario(world: World) -> None:
        _, raw = await world.convert()
        await world.receive(
            {
                "heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}],
                "sleep": [{"session_end_time": "2026-05-08T06:00:00Z"}],
            }
        )
        world.obsidian.write_binary_file.reset_mock()

        await world.engine.run_by_hand(raw.id, world.user)

        assert sorted(world.files_written()) == [
            "Health/raw/heart_rate/2026-05-09.jsonl",
            "Health/raw/sleep/2026-05-08.jsonl",
        ]
        assert world.said() == [
            "Health raw data → vault files: 2 runs. 2 finished, 0 stopped, 0 failed."
        ]

    _run(scenario)


def test_the_workflows_are_commands_with_the_names_the_plugin_used() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        user = mock_user_find_by_id(4)
        user.plugins = ["workflows"]

        assert [command.text for command in commands.commands_for_user(user)] == [
            "Sync health data",
            "Export Health Connect raw data",
        ]

    _run(scenario)


# -- a user who had the plugin before ------------------------------------------


async def _save_old_config(world: World, **settings: Any) -> None:
    await world.redis.set(CONFIG_KEY, json.dumps({"token": "abc", **settings}))


def test_the_conversion_gives_both_workflows_with_the_recipes_settings() -> None:
    async def scenario(world: World) -> None:
        await _save_old_config(world)

        daily, raw = await world.convert()

        assert (daily.name, raw.name) == (
            "Health data → daily note",
            "Health raw data → vault files",
        )
        assert daily.enabled and raw.enabled
        properties = daily.steps[1].inputs["properties"]
        assert list(properties) == [
            "steps",
            "resting-heart-rate",
            "heart-rate-p5",
            "heart-rate-avg",
            "heart-rate-p95",
            "heart-rate-p98",
            "heart-rate-p99",
            "hr-moderate-minutes",
            "hr-vigorous-minutes",
            "vo2-max",
        ]
        assert raw.steps[0].inputs["path"] == (
            "Health/raw/{{ trigger.type }}/{{ trigger.date }}.jsonl"
        )

    _run(scenario)


def test_the_conversion_keeps_the_folder_and_the_properties_that_were_on() -> None:
    async def scenario(world: World) -> None:
        await _save_old_config(
            world,
            raw_data_folder="Data/Health Connect",
            daily_note_fields={
                "steps": False,
                "vo2_max": False,
                "heart_rate_avg": True,
            },
        )

        daily, raw = await world.convert()
        await world.receive({"steps": [_steps_record("a", 8, 100)]})

        properties = daily.steps[1].inputs["properties"]
        assert "steps" not in properties and "vo2-max" not in properties
        assert len(properties) == 8
        # Steps were turned off, so the payload writes no property.
        assert world.properties_set() == []
        assert list(world.files_written()) == [
            "Data/Health Connect/steps/2026-05-09.jsonl"
        ]
        # The saved config keeps only what is still a setting of the plugin.
        assert json.loads(world.redis.strings[CONFIG_KEY]) == {"token": "abc"}
        assert await HealthConnectPlugin.get_config(world.user) == UserConfig(
            token="abc"
        )

    _run(scenario)


def test_the_conversion_turns_the_daily_note_workflow_off_if_nothing_was_written() -> (
    None
):
    async def scenario(world: World) -> None:
        every_property_off = {
            name.replace("-", "_"): False
            for name in (await world.convert())[0].steps[1].inputs["properties"]
        }
        # Start over, as a user who had turned every property off.
        for workflow in await world.engine.store.list_for_user(4):
            await world.engine.delete(workflow)
        await _save_old_config(world, daily_note_fields=every_property_off)

        daily, raw = await world.convert()

        assert not daily.enabled
        assert raw.enabled
        # Its properties are all there, for when it is turned on.
        assert len(daily.steps[1].inputs["properties"]) == 10

    _run(scenario)


def test_the_conversion_queues_what_the_plugin_had_not_written_yet() -> None:
    async def scenario(world: World) -> None:
        await _save_old_config(world)
        store = HealthConnectStore(world.user)
        today = datetime.datetime.now(tz=datetime.timezone.utc).date()
        # A raw data file that the plugin still had to write.
        await store.add_records("sleep", [{"session_end_time": "2026-04-30T06:00:00Z"}])
        await world.redis.sadd("health-connect:export-pending:4", "sleep|2026-04-30")
        # A payload that arrived today, before the workflows were there.
        await store.add_records(
            "steps",
            [
                {
                    "count": 420,
                    "start_time": f"{today.isoformat()}T08:00:00Z",
                    "end_time": f"{today.isoformat()}T08:10:00Z",
                }
            ],
        )

        await world.convert()
        await world.settle()

        assert world.properties_set() == [
            (f"Daily/{today.isoformat()}.md", "steps", 420)
        ]
        assert sorted(world.files_written()) == sorted(
            [
                "Health/raw/sleep/2026-04-30.jsonl",
                f"Health/raw/steps/{today.isoformat()}.jsonl",
            ]
        )
        assert "health-connect:export-pending:4" not in world.redis.sets

    _run(scenario)


def test_the_conversion_done_again_installs_nothing() -> None:
    async def scenario(world: World) -> None:
        await _save_old_config(world)
        await world.convert()

        assert await convert_to_workflows(world.engine, world.user) == []
        assert len(await world.engine.store.list_for_user(4)) == 2

    _run(scenario)


def test_a_user_of_the_plugin_is_converted_once_at_startup_and_told() -> None:
    async def scenario(world: World) -> None:
        await _save_old_config(world)
        await world.redis.sadd("config:plugin:name=health-connect:users", 4)

        with patch(
            "spanreed.workflows.migrations.User.find_by_id",
            new=AsyncMock(side_effect=mock_user_find_by_id),
        ):
            await migrations.convert_plugins(world.engine)
            await migrations.convert_plugins(world.engine)

        assert len(await world.engine.store.list_for_user(4)) == 2
        world.bot.notify.assert_awaited_once()
        message = world.bot.notify.await_args.args[0]
        assert message.startswith(
            "Health Connect now does its work through workflows: “Health data → "
            "daily note” and “Health raw data → vault files”."
        )
        assert "“Sync health data”" in message
        # The user stays a user of the plugin: it still receives the data.
        assert await world.redis.smembers(
            "config:plugin:name=health-connect:users"
        ) == {"4"}

    _run(scenario)


def test_a_workflow_that_the_user_deleted_does_not_come_back() -> None:
    async def scenario(world: World) -> None:
        await _save_old_config(world)
        await world.redis.sadd("config:plugin:name=health-connect:users", 4)

        with patch(
            "spanreed.workflows.migrations.User.find_by_id",
            new=AsyncMock(side_effect=mock_user_find_by_id),
        ):
            await migrations.convert_plugins(world.engine)
            for workflow in await world.engine.store.list_for_user(4):
                await world.engine.delete(workflow)
            # The next start of Spanreed.
            await migrations.convert_plugins(world.engine)

        assert await world.engine.store.list_for_user(4) == []

    _run(scenario)
