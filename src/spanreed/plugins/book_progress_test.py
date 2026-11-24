"""Audible progress from end to end: the notes, the library, the questions.

The engine, the Audible blocks, the memory and the Obsidian blocks are the
real ones. Redis is in memory, and the vault, Audible and the user are
mocks. The cases are the ones the Audible Book Progress plugin was run on
before it became a workflow (see ``book_progress_test_data``).
"""

import asyncio
import contextlib
import datetime
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.audible import AudibleBook, AudiblePlugin
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import UserInteractionPriority, commands
from spanreed.interaction import engine as turns
from spanreed.plugin import Plugin
from spanreed.plugins.book_progress import CANONICAL_NAME, RECIPE
from spanreed.plugins.book_progress_test_data import CASES, OLD_PLUGIN
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.builtin import Tick
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow

USERS_KEY = f"config:plugin:name={CANONICAL_NAME}:users"
DATA_PREFIX = f"plugin-data:plugin:name={CANONICAL_NAME}:user_id=4:"
NOW = datetime.datetime(2026, 11, 24, 10, 0, tzinfo=datetime.timezone.utc)
TODAY = "2026-11-24"


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    obsidian: AsyncMock
    audible: AsyncMock
    bot: AsyncMock
    user: Any
    # The notes the query finds, as ``[path, title, asin]``, the library,
    # as ``[asin, title, percent, finished]``, and what the notes hold, by
    # ``path|property``.
    notes: list[list[Any]] = field(default_factory=list)
    library: list[list[Any]] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    answers: list[int] = field(default_factory=list)
    # What was done, as the test data holds it.
    lines: list[list[Any]] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)

    def given(self, case: dict[str, Any]) -> None:
        """Set up what a case of the test data says there was."""
        self.notes = case["notes"]
        self.library = case["library"]
        self.properties = dict(case.get("properties", {}))
        self.answers = list(case.get("answers", []))
        self.lines = []

    async def register(self) -> None:
        await self.redis.sadd(USERS_KEY, "4")
        await self.redis.sadd("user:4:plugins", CANONICAL_NAME)

    async def retire(self) -> Workflow:
        """Move the user to the workflow, as the start of Spanreed does."""
        await self.register()
        await migrations.retire_plugins(self.engine)
        (workflow,) = await self.engine.store.list_for_user(4)
        return workflow

    async def new_workflow(self) -> Workflow:
        """Return a workflow of the recipe that remembers nothing yet."""
        for old in await self.engine.store.list_for_user(4):
            await self.engine.delete(old)
        return await self.retire()

    async def run_command(self, workflow: Workflow) -> dict[str, Any]:
        """Run the workflow as its command does, and return the trace."""
        async with asyncio.timeout(3):
            async with turns.user_interaction(4, priority=UserInteractionPriority.HIGH):
                await self.engine.run_by_hand(workflow.id, self.user)
        (run,) = await self.engine.store.recent_runs(workflow.id, 1)
        return run

    async def run_by_itself(self, workflow: Workflow) -> dict[str, Any]:
        """Run the workflow as its schedule does, and return the trace."""
        tick = Tick(date=TODAY, time="12:00")
        async with asyncio.timeout(3):
            return await self.engine._run(
                workflow, self.user, tick, manual=False, attempt=1
            )


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), AudiblePlugin()]
    )
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    audible = AsyncMock()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.book_progress.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch.object(Plugin, "is_registered", async_return_true),
        # The schedule is not due for a long time: a test says when a run is.
        patch(
            "spanreed.workflows.store.StoredTriggerState.get",
            new=AsyncMock(return_value="2999-01-01T00:00:00+00:00"),
        ),
        patch("spanreed.workflows.builtin._now", return_value=NOW),
        patch(
            "spanreed.apis.audible_blocks.AudibleApi.for_user",
            new=AsyncMock(return_value=audible),
        ),
        patch("spanreed.apis.audible_blocks.ObsidianApi", autospec=True) as vault,
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", new=vault),
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.blocks.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            obsidian=vault,
            audible=audible,
            bot=bot,
            user=mock_user_find_by_id(4),
        )

        async def query(text: str) -> list[Any]:
            world.lines.append(["query", text])
            return [
                SimpleNamespace(
                    file={"path": path},
                    title=title,
                    asin=world.properties.get(f"{path}|asin", asin),
                )
                for path, title, asin in world.notes
            ]

        async def library() -> list[AudibleBook]:
            world.lines.append(["library"])
            return [
                AudibleBook(asin, title, None, percent, finished)
                for asin, title, percent, finished in world.library
            ]

        async def get(note: str, name: str) -> Any:
            world.lines.append(["get", note, name])
            return world.properties.get(f"{note}|{name}")

        async def set_(note: str, name: str, value: Any) -> None:
            world.lines.append(["set", note, name, value])
            world.properties[f"{note}|{name}"] = value

        async def choice(prompt: str, choices: list[Any], **how: Any) -> int:
            answer = world.answers.pop(0)
            world.lines.append(["choice", prompt, list(choices), answer])
            return answer

        async def say(message: str, **how: Any) -> None:
            world.lines.append(["say", message])

        async def notify(message: str, **how: Any) -> None:
            world.notified.append(message)

        real_set = workflow_store.StoredMemory.set

        async def remember(memory: Any, name: str, value: Any = True) -> None:
            if name != migrations.CARRIED_OVER:
                world.lines.append(["remember", name])
            await real_set(memory, name, value)

        vault.for_user = AsyncMock(return_value=vault)
        vault.query_dataview = AsyncMock(side_effect=query)
        vault.get_property = AsyncMock(side_effect=get)
        vault.set_value_of_property = AsyncMock(side_effect=set_)
        audible.get_library = AsyncMock(side_effect=library)
        bot.for_user = AsyncMock(return_value=bot)
        bot.request_user_choice = AsyncMock(side_effect=choice)
        bot.send_message = AsyncMock(side_effect=say)
        bot.notify = AsyncMock(side_effect=notify)
        with patch.object(workflow_store.StoredMemory, "set", new=remember):
            yield world


def _run(scenario: Any) -> None:
    with _world() as world:

        async def run() -> None:
            try:
                await scenario(world)
            finally:
                await world.engine.stop()

        asyncio.run(run())


def _as_the_workflow_does(lines: list[list[Any]]) -> list[list[Any]]:
    """Return what the plugin did, with what the workflow does on purpose.

    The query is the same text without the plugin's indentation. The
    date a book was finished is the date of the run in the user's
    timezone. And the two messages of the plugin's command are not sent:
    see ``test_a_run_by_hand_says_how_it_went_only_if_nothing_was_asked``.
    """
    expected = []
    for line in lines:
        if line[0] == "notify":
            continue
        if line[0] == "query":
            line = ["query", _plain(line[1])]
        expected.append([TODAY if item == "<today>" else item for item in line])
    return expected


def _plain(query: str) -> str:
    return re.sub(r"\s*\n\s*", "\n", query).strip()


def test_the_workflow_does_what_the_plugin_did() -> None:
    async def scenario(world: World) -> None:
        for name, case in CASES.items():
            # Each case starts with nothing remembered, as the plugin did.
            workflow = await world.new_workflow()
            assert world.engine.problems(workflow) == []
            world.given(case)
            for number in range(case.get("syncs", 1)):
                if number:
                    world.lines.append(["again"])
                if case.get("by_hand"):
                    run = await world.run_command(workflow)
                else:
                    run = await world.run_by_itself(workflow)
                assert run["status"] in ("finished", "stopped"), name

            did = [line for line in world.lines if line[0] != "say"]
            for line in did:
                if line[0] == "query":
                    line[1] = _plain(line[1])
            assert did == _as_the_workflow_does(OLD_PLUGIN[name]), name
            assert world.answers == [], name

    _run(scenario)


def test_a_run_by_hand_says_how_it_went_only_if_nothing_was_asked() -> None:
    """The plugin's command said "Syncing…" and "No changes" instead."""

    async def scenario(world: World) -> None:
        workflow = await world.retire()
        assert commands.get_command(f"workflow.{workflow.id}") is not None

        world.given(CASES["by hand, nothing changed"])
        await world.run_command(workflow)
        assert [line for line in world.lines if line[0] == "say"] == [
            ["say", "Audible progress → book notes: finished."]
        ]

        world.given(CASES["unlinked, book chosen"])
        await world.run_command(workflow)
        assert [line for line in world.lines if line[0] == "say"] == []

    _run(scenario)


def test_a_sync_that_asks_nothing_does_not_take_the_turn_to_talk() -> None:
    """It runs every hour, and asks about a book once in a long while."""

    async def scenario(world: World) -> None:
        workflow = await world.retire()
        world.given(CASES["linked, progress changed"])

        # Another conversation is open. The sync does not wait for it.
        async with turns.user_interaction(4):
            run = await world.run_by_itself(workflow)

        assert run["status"] == "finished"
        assert ["set", "Books/Kings.md", "progress", 42] in world.lines
        assert "talked" not in run

    _run(scenario)


def test_ask_me_later_asks_again_after_a_day() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.retire()
        case = CASES["unlinked, ask me later, then again"]
        world.given({**case, "answers": [2, 0]})

        def asked() -> int:
            return len([line for line in world.lines if line[0] == "choice"])

        with patch("spanreed.apis.audible_blocks._now") as now:
            now.return_value = datetime.datetime.now(datetime.timezone.utc)
            await world.run_by_itself(workflow)
            assert asked() == 1

            now.return_value += datetime.timedelta(hours=23)
            await world.run_by_itself(workflow)
            assert asked() == 1

            now.return_value += datetime.timedelta(hours=2)
            await world.run_by_itself(workflow)
            assert asked() == 2
        assert world.properties["Books/Kings.md|asin"] == "B1"

    _run(scenario)


def test_the_history_shows_the_library_as_a_count() -> None:
    """A library has hundreds of books, and every note's step gets it."""

    async def scenario(world: World) -> None:
        workflow = await world.retire()
        world.given(CASES["linked, progress changed"])

        run = await world.run_by_itself(workflow)

        notes, _any, audible, each = run["steps"]
        assert audible["outputs"] == {"library": "<2 Audible books>", "count": 2}
        (kept,) = each["rounds"]
        book = kept["steps"][0]
        assert book["inputs"]["library"] == "<2 Audible books>"
        assert book["outputs"] == {
            "linked": True,
            "outcome": "known",
            "asin": "B1",
            "title": "The Way of Kings: The Stormlight Archive, Book 1",
            "percent": 42,
            "finished": False,
        }

    _run(scenario)


# -- from the plugin to the workflow -------------------------------------------


def test_the_user_gets_the_workflow_and_what_the_plugin_remembered() -> None:
    async def scenario(world: World) -> None:
        await world.redis.set(DATA_PREFIX + "not-audible:Books/Kings.md", "true")
        await world.redis.set(DATA_PREFIX + "finish-prompted:B2", "2026-10-01T09:00:00")
        await world.redis.set(DATA_PREFIX + "link-snooze:Books/Emma.md", "2026-11-24")
        await world.redis.set(DATA_PREFIX + "something-else", "x")

        workflow = await world.retire()

        assert (workflow.recipe, workflow.enabled) == (RECIPE, True)
        command = commands.get_command(f"workflow.{workflow.id}")
        assert command is not None and command.text == "Sync book progress"
        memory = world.engine.store.memory(workflow.id)
        assert await memory.names() == [
            "finish-prompted:B2",
            "link-snooze:Books/Emma.md",
            migrations.CARRIED_OVER,
            "not-audible:Books/Kings.md",
        ]
        # The plugin is gone for the user, who is told once.
        assert await world.redis.smembers(USERS_KEY) == set()
        (told,) = world.notified
        assert "“Audible Book Progress” is now a workflow" in told
        assert "It knows which books you said are not on Audible" in told

        # So the user is not asked again: not which book the note is,
        # and not whether to mark the finished one as read.
        world.given(
            {
                "notes": [
                    ["Books/Kings.md", "The Way of Kings", None],
                    ["Books/Mary.md", "Project Hail Mary", "B2"],
                ],
                "library": [
                    ["B1", "The Way of Kings", 10.0, False],
                    ["B2", "Project Hail Mary", 100.0, True],
                ],
            }
        )
        run = await world.run_by_itself(workflow)
        assert run["status"] == "finished"
        assert [line for line in world.lines if line[0] == "choice"] == []
        assert ["set", "Books/Mary.md", "progress", 100] in world.lines

        await migrations.retire_plugins(world.engine)
        assert len(await world.engine.store.list_for_user(4)) == 1
        assert len(world.notified) == 1

    _run(scenario)


def test_a_move_that_stopped_halfway_is_finished_at_the_next_start() -> None:
    """The workflow stays off until it knows what the plugin knew."""

    async def scenario(world: World) -> None:
        await world.redis.set(DATA_PREFIX + "finish-prompted:B2", "2026-10-01")
        await world.register()

        with patch(
            "spanreed.plugins.book_progress.redis_api.scan_iter",
            side_effect=RuntimeError("Redis went away"),
        ):
            await migrations.retire_plugins(world.engine)

        (workflow,) = await world.engine.store.list_for_user(4)
        assert workflow.enabled is False
        assert await world.engine.store.memory(workflow.id).names() == []
        # Still registered, so the next start tries again.
        assert await world.redis.smembers(USERS_KEY) == {"4"}

        await migrations.retire_plugins(world.engine)

        (workflow,) = await world.engine.store.list_for_user(4)
        assert workflow.enabled is True
        assert await world.engine.store.memory(workflow.id).names() == [
            "finish-prompted:B2",
            migrations.CARRIED_OVER,
        ]
        assert await world.redis.smembers(USERS_KEY) == set()

    _run(scenario)


def test_a_workflow_the_user_turned_off_stays_off() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.retire()
        workflow.enabled = False
        await world.engine.save(workflow)

        # Registered again by hand, as after restoring a backup.
        await world.register()
        await migrations.retire_plugins(world.engine)

        (workflow,) = await world.engine.store.list_for_user(4)
        assert workflow.enabled is False

    _run(scenario)
