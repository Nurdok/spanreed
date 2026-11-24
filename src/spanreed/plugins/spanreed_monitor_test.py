"""What Spanreed Monitor told its users, from end to end, as workflows.

The engine and the blocks are the real ones. Redis is in memory, and the
user is a mock. The cases are the ones the plugin was run on before it
became workflows (see ``spanreed_monitor_test_data``).
"""

import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed import errors
from spanreed.apis import obsidian_watch
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import commands
from spanreed.interaction import engine as turns
from spanreed.plugin import Plugin
from spanreed.plugins.spanreed_monitor import (
    CANONICAL_NAME,
    RECIPES,
    WATCHDOG_RECIPE,
    move,
)
from spanreed.plugins.spanreed_monitor_test_data import EXCEPTIONS, OBSIDIAN, OLD_PLUGIN
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.workflows import migrations, system
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow

USERS_KEY = f"config:plugin:name={CANONICAL_NAME}:users"
START = datetime.datetime(2026, 11, 24, 8, 0, tzinfo=datetime.timezone.utc)
PLAIN = {"parse_html": False, "parse_markdown": False}


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    bot: AsyncMock
    user: Any
    # What the user was told and what was dropped, as the test data holds it.
    lines: list[list[Any]] = field(default_factory=list)
    # The errors that Spanreed kept and that wait, newest first.
    waiting: list[str] = field(default_factory=list)
    now: datetime.datetime = START

    async def has(self, *plugins: str) -> None:
        """Register the user to the monitor plugin and to these plugins."""
        await self.redis.sadd(USERS_KEY, 4)
        # Every user is registered to the plugin that runs the workflows.
        await self.redis.sadd("user:4:plugins", CANONICAL_NAME, "workflows", *plugins)

    async def workflows(self) -> dict[str | None, Workflow]:
        """Return the workflows of the user, by their recipe."""
        return {w.recipe: w for w in await self.engine.store.list_for_user(4)}

    def told(self) -> list[str]:
        """Return the texts the user was told, in order."""
        return [line[1] for line in self.lines if line[0] in ("notify", "send")]

    async def until(self, predicate: Any) -> None:
        """Wait until the runs made something true."""
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0)

    async def pass_errors_on(self) -> None:
        """Hand the errors that wait to the workflows, as the engine's plugin does."""

        async def users() -> list[Any]:
            return [self.user]

        with pytest.raises(asyncio.CancelledError):
            await system.pass_errors_on(users)


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()
    obsidian_watch.reset_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins([WorkflowsPlugin(), ObsidianPlugin()])
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.blocks.UserInteractionApi", new=bot),
        patch("spanreed.plugins.spanreed_monitor.UserInteractionApi", new=bot),
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            bot=bot,
            user=mock_user_find_by_id(4),
        )

        async def notify(text: str, **how: Any) -> None:
            world.lines.append(["notify", text, how])

        async def send_message(text: str, **how: Any) -> None:
            world.lines.append(["send", text, how])

        async def next_report() -> str:
            if not world.waiting:
                # The end of the test's errors, not a failure to read them.
                raise asyncio.CancelledError()
            return world.waiting.pop(0)

        async def drop_waiting() -> None:
            world.lines.append(["dropped", list(world.waiting)])
            world.waiting.clear()

        bot.for_user = AsyncMock(return_value=bot)
        bot.notify = AsyncMock(side_effect=notify)
        bot.send_message = AsyncMock(side_effect=send_message)
        with (
            patch("spanreed.workflows.system.errors.next_report", new=next_report),
            patch("spanreed.workflows.system.errors.drop_waiting", new=drop_waiting),
            patch("spanreed.workflows.system._now", lambda: world.now),
            patch("spanreed.apis.obsidian_watch._now", lambda: world.now),
        ):
            yield world


# -- the move ------------------------------------------------------------------


def test_a_user_of_the_plugin_gets_its_workflows_once() -> None:
    async def scenario(world: World) -> None:
        await world.has("obsidian")

        await migrations.retire_plugins(world.engine)

        workflows = await world.workflows()
        assert set(workflows) == {*RECIPES, WATCHDOG_RECIPE}
        assert {w.name: w.enabled for w in workflows.values()} == {
            "Spanreed started": True,
            "Spanreed had an error": True,
            # The plugin never said it, so it does not start by itself.
            "Obsidian stopped answering": False,
        }
        assert all(not world.engine.problems(w) for w in workflows.values())
        # There is nothing to fetch, so none of them is a command.
        assert not commands.commands_for_user(world.user)
        assert world.lines == [
            [
                "notify",
                "The plugin “Spanreed Monitor” is now workflows: “Spanreed "
                "started” and “Spanreed had an error”. They tell you what the "
                "plugin told you, in the same words. Change what they say, or "
                "turn one off, under Workflows in the web UI. “Obsidian stopped "
                "answering” is new, and off: the plugin was meant to tell you "
                "when Obsidian went quiet, and never did. Turn it on if you "
                "want to hear of it. Nothing says “Spanreed is shutting down.” "
                "any more.",
                {"parse_html": False},
            ]
        ]
        # The user is no longer registered to the plugin.
        assert await world.redis.smembers("user:4:plugins") == {
            "workflows",
            "obsidian",
        }
        assert not await world.redis.smembers(USERS_KEY)

        # Doing it again changes nothing, also for a start that stopped
        # before the registration was dropped.
        await world.redis.sadd(USERS_KEY, 4)
        await migrations.retire_plugins(world.engine)
        assert len(await world.workflows()) == 3
        assert len(world.lines) == 1
        await world.engine.stop()

    with _world() as world:
        asyncio.run(scenario(world))


def test_a_user_without_obsidian_gets_no_workflow_for_it() -> None:
    async def scenario(world: World) -> None:
        await world.has()

        made = await move(world.engine, world.user)

        assert [w.recipe for w in made] == list(RECIPES)
        (text,) = world.told()
        assert "Obsidian" not in text
        assert text.endswith("Nothing says “Spanreed is shutting down.” any more.")
        await world.engine.stop()

    with _world() as world:
        asyncio.run(scenario(world))


def test_a_move_that_stopped_halfway_makes_only_what_is_missing() -> None:
    async def scenario(world: World) -> None:
        await world.has("obsidian")
        await move(world.engine, world.user)
        started = (await world.workflows())["spanreed-started"]
        await world.engine.delete(started)
        world.lines.clear()

        made = await move(world.engine, world.user)

        assert [w.recipe for w in made] == ["spanreed-started"]
        assert len(await world.workflows()) == 3
        await world.engine.stop()

    with _world() as world:
        asyncio.run(scenario(world))


# -- what Spanreed tells, as the plugin did -------------------------------------


def _as_workflows(did: list[list[Any]]) -> list[list[Any]]:
    """Return what the plugin did, as the workflows do it.

    These are the differences, and they are meant:

    - A text is sent as it is written. The plugin's texts hold no HTML, so
      they look the same.
    - The errors that wait behind the newest are dropped when it is taken,
      not after the user was told about it.
    - Nothing says that Spanreed is shutting down.
    """
    lines: list[list[Any]] = []
    for line in did:
        if line[0] == "send" and line[1] == "Spanreed is shutting down.":
            continue
        if line[0] == "notify":
            line = ["notify", line[1], {**PLAIN, **line[2]}]
        lines.append(line)
    for place, line in enumerate(lines):
        if line[0] == "dropped":
            # From behind the two notifications of its error to before them.
            lines.insert(place - 2, lines.pop(place))
    return lines


@pytest.mark.parametrize("name", EXCEPTIONS)
def test_the_workflows_tell_what_the_plugin_told(name: str) -> None:
    case = EXCEPTIONS[name]
    expected = _as_workflows(OLD_PLUGIN["exceptions"][name])

    async def scenario(world: World) -> None:
        await world.has()
        await move(world.engine, world.user)
        world.lines.clear()

        # Spanreed starts, with what waited from before.
        world.waiting = list(case["waiting"])
        await system.say_started([world.user])
        # The two are workflows of their own, so which of them tells first
        # is open. The plugin said that it started first: wait for that.
        await world.until(lambda: world.told())
        await world.pass_errors_on()
        # What comes while it runs.
        world.waiting = list(case.get("later", []))
        await world.pass_errors_on()

        await world.until(lambda: len(world.lines) >= len(expected))
        for _ in range(20):
            await asyncio.sleep(0)
        assert world.lines == expected
        # Stopping tells nothing, for the case that was stopped too.
        await world.engine.stop()
        assert world.lines == expected

    with _world() as world:
        asyncio.run(scenario(world))


def test_the_start_is_told_with_its_time_where_a_workflow_asks_for_it() -> None:
    async def scenario(world: World) -> None:
        await world.has()
        await move(world.engine, world.user)
        started = (await world.workflows())["spanreed-started"]
        started.steps[0].inputs[
            "message"
        ] = "Up since {{ trigger.date }} {{ trigger.time }}."
        await world.engine.save(started)
        world.lines.clear()

        await system.say_started([world.user])
        await world.until(lambda: world.told())

        assert world.told() == ["Up since 2026-11-24 08:00."]
        await world.engine.stop()

    with _world() as world:
        asyncio.run(scenario(world))


# -- what Obsidian's plugin says ------------------------------------------------


async def _plugin_says(world: World, said: list[tuple[float, Any]]) -> None:
    """Take in what the plugin for Obsidian said, each thing after its minutes."""
    obsidian_watch.vault_of(4)
    for minutes, thing in said:
        world.now += datetime.timedelta(minutes=minutes)
        if thing is not None:
            await obsidian_watch.take(4, json.dumps(thing))


@pytest.mark.parametrize("name", OBSIDIAN)
def test_the_errors_of_obsidians_plugin_are_reported_as_the_plugin_did(
    name: str,
) -> None:
    reported: list[list[Any]] = []

    async def report(text: str) -> None:
        minute = (world.now - START).total_seconds() / 60
        reported.append(["reported", minute, text])

    async def scenario(world: World) -> None:
        await world.has("obsidian")
        await move(world.engine, world.user)
        world.lines.clear()

        await _plugin_says(world, OBSIDIAN[name])
        for _ in range(20):
            await asyncio.sleep(0)

        assert reported == OLD_PLUGIN["obsidian"][name]
        # Nothing was told about a vault that went quiet, as before: the
        # workflow for that is off.
        assert world.lines == []
        await world.engine.stop()

    with (
        _world() as world,
        patch("spanreed.apis.obsidian_watch.errors.report", new=report),
    ):
        asyncio.run(scenario(world))


def test_an_error_of_obsidians_plugin_reaches_the_user_in_the_plugins_words() -> None:
    fake = FakeRedis()

    async def scenario(world: World) -> None:
        await world.has("obsidian")
        await move(world.engine, world.user)
        world.lines.clear()

        await _plugin_says(world, OBSIDIAN["an error after the first half hour"])
        # From the list of errors on, as the engine's plugin reads it.
        world.waiting = list(fake.lists[errors.QUEUE])
        await world.pass_errors_on()
        await world.until(lambda: len(world.told()) == 2)

        assert world.told() == [
            "Exception retrieved from storage:\n\n```python\n"
            "Obsidian plugin error: {'kind': 'error', 'message': 'Vault is locked'}"
            "\n```",
            "Spanreed is still running.",
        ]
        assert world.lines[1][2] == {"parse_html": False, "parse_markdown": True}
        await world.engine.stop()

    with _world() as world, patch("spanreed.errors.redis_api", new=fake):
        asyncio.run(scenario(world))


def test_the_workflow_for_a_quiet_vault_tells_once_it_is_turned_on() -> None:
    async def sleep(seconds: float) -> None:
        world.now += datetime.timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def scenario(world: World) -> None:
        await world.has("obsidian")
        await move(world.engine, world.user)
        world.lines.clear()
        watchdog = (await world.workflows())[WATCHDOG_RECIPE]

        watchdog.enabled = True
        await world.engine.save(watchdog)
        await world.until(lambda: len(world.told()) >= 2)
        await world.engine.stop()

        # After a minute without a heartbeat, and again half an hour later.
        assert world.lines[:2] == [
            ["send", "Obsidian plugin watchdog timeout.", PLAIN],
            ["send", "Obsidian plugin watchdog timeout.", PLAIN],
        ]
        runs = await world.engine.store.recent_runs(watchdog.id, 100)
        assert [run["trigger"]["outputs"] for run in reversed(runs)][:2] == [
            {"last_seen": "", "minutes": 1},
            {"last_seen": "", "minutes": 31},
        ]

    with (
        _world() as world,
        patch("spanreed.apis.obsidian_watch._sleep", new=sleep),
        patch("spanreed.apis.obsidian_watch.start_reading") as start_reading,
    ):
        asyncio.run(scenario(world))

    # The trigger sees to it that what Obsidian's plugin says is read.
    assert start_reading.call_args.args[0].id == 4
