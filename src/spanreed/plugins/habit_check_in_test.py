"""The habit check-in from end to end: the question, the picks, the daily note.

The engine, the asking block and the Obsidian blocks are the real ones.
Redis is in memory, and Obsidian and the user are mocks. The days are the
ones the Habit Tracker plugin was run on before it became a workflow (see
``habit_check_in_test_data``).
"""

import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.obsidian import DailyNote, ObsidianPlugin
from spanreed.interaction import UserInteractionPriority, commands
from spanreed.interaction import engine as turns
from spanreed.plugin import Plugin
from spanreed.plugins.habit_check_in import RECIPE, keep_settings, options_for
from spanreed.plugins.habit_check_in_test_data import (
    HABITS,
    HELD,
    MISSING,
    NOTE,
    OLD_PLUGIN,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import (
    FakeRedis,
    async_return_true,
    daily_note_in_daily_folder,
    mock_user_find_by_id,
)
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.builtin import Tick
from spanreed.workflows.engine import TOOK_OVER, WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

CONFIG_KEY = "config:plugin:name=habit-tracker:user_id=4"
USERS_KEY = "config:plugin:name=habit-tracker:users"
QUESTION = "Did you do any of these habits today?"
# The day of the note in the test data, at a time that is that day anywhere
# a test user lives.
NOW = datetime.datetime(2026, 11, 24, 10, 0, tzinfo=datetime.timezone.utc)


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    obsidian: AsyncMock
    bot: AsyncMock
    user: Any
    # What the property of the daily note holds. ``MISSING``: the note is
    # not there the first time it is read.
    held: Any = None
    # What the user answers, first answer first. A future is a question
    # that waits for its answer.
    answers: list[Any] = field(default_factory=list)
    # What was read, asked, written and said, as the test data holds it.
    lines: list[list[Any]] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)
    # How many times the property was read.
    reads: int = 0

    async def save_old_settings(
        self,
        folder: str = "Daily",
        name: str = "habits",
        habits: list[str] | None = None,
    ) -> None:
        """Save what the plugin had, and register the user to it."""
        config = {
            "daily_note_path": folder,
            "habit_tracker_property_name": name,
            "habits": [
                {"name": habit, "description": f"About {habit}"}
                for habit in (HABITS if habits is None else habits)
            ],
        }
        await self.redis.set(CONFIG_KEY, json.dumps(config))
        await self.register()

    async def register(self) -> None:
        await self.redis.sadd(USERS_KEY, "4")
        await self.redis.sadd("user:4:plugins", "habit-tracker")

    async def retire(self) -> Workflow:
        """Move the user to the workflow, as the start of Spanreed does."""
        if await self.redis.get(CONFIG_KEY) is None:
            await self.save_old_settings()
        await migrations.retire_plugins(self.engine)
        (workflow,) = await self.engine.store.list_for_user(4)
        return workflow

    async def run_command(self, workflow: Workflow) -> dict[str, Any]:
        """Run the workflow as its command does, and return the trace."""
        async with asyncio.timeout(3):
            async with turns.user_interaction(4, priority=UserInteractionPriority.HIGH):
                await self.engine.run_by_hand(workflow.id, self.user)
        (run,) = await self.engine.store.recent_runs(workflow.id, 1)
        return run


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins([WorkflowsPlugin(), ObsidianPlugin()])
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    with (
        patch("spanreed.plugin.redis_api", new=fake),
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
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as obsidian,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.blocks.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            obsidian=obsidian,
            bot=bot,
            user=mock_user_find_by_id(4),
        )

        async def create(date: datetime.date) -> DailyNote:
            world.lines.append(["create"])
            return await daily_note_in_daily_folder(date)

        async def get(note: str, name: str) -> Any:
            world.lines.append(["get", note, name])
            world.reads += 1
            if world.held == MISSING:
                if world.reads == 1:
                    raise FileNotFoundError(note)
                return None
            return world.held

        async def add(note: str, name: str, value: Any) -> None:
            world.lines.append(["add", note, name, value])

        async def choice(prompt: str, choices: list[Any], **how: Any) -> int:
            answer = world.answers.pop(0)
            line = ["choice", prompt, list(choices), how.get("columns", 1)]
            if not isinstance(answer, int):
                world.lines.append([*line, None])
                return int(await answer)
            world.lines.append([*line, answer])
            return answer

        async def say(message: str, **how: Any) -> None:
            world.lines.append(["say", message])

        async def notify(message: str, **how: Any) -> None:
            world.notified.append(message)

        obsidian.for_user = AsyncMock(return_value=obsidian)
        obsidian.create_daily_note = AsyncMock(side_effect=create)
        obsidian.get_property = AsyncMock(side_effect=get)
        obsidian.add_value_to_list_property = AsyncMock(side_effect=add)
        bot.for_user = AsyncMock(return_value=bot)
        bot.request_user_choice = AsyncMock(side_effect=choice)
        bot.send_message = AsyncMock(side_effect=say)
        bot.notify = AsyncMock(side_effect=notify)
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

    The workflow sends neither of the plugin's two messages. A property
    that is not a list counts as no habits done, without a word. A daily
    note that is not there after it was asked for counts as one without
    the property, and is not asked for a second time.

    And a run by hand that asked nothing says that it finished, where the
    plugin's command said nothing at all.
    """
    expected: list[list[Any]] = []
    skip = 0
    for line in lines:
        if skip:
            skip -= 1
            continue
        if line[0] == "say":
            if line[1].startswith("Generating"):
                # The plugin then asked for the note and read it again.
                skip = 2
            continue
        expected.append(line)
    if not any(line[0] == "choice" for line in expected):
        expected.append(["say", "Habit check-in: finished."])
    return expected


def _answers(lines: list[list[Any]]) -> list[Any]:
    return [line[-1] for line in lines if line[0] == "choice"]


def test_the_workflow_reads_asks_and_writes_what_the_plugin_did() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.retire()
        assert world.engine.problems(workflow) == []
        command = commands.get_command(f"workflow.{workflow.id}")
        assert command is not None and command.text == "Track Habits"

        for name, old in OLD_PLUGIN.items():
            world.held = HELD[name]
            world.answers = _answers(old)
            world.lines = []
            world.reads = 0

            run = await world.run_command(workflow)

            assert world.lines == _as_the_workflow_does(old), name
            assert world.answers == [], name
            assert run["status"] == "finished", name
        # The note of the day, in the folder the plugin had.
        assert NOTE == f"Daily/{NOW.date().isoformat()}.md"

    _run(scenario)


def test_a_check_in_with_nothing_left_to_ask_says_so_when_run_by_hand() -> None:
    """The plugin's command said nothing at all then."""

    async def scenario(world: World) -> None:
        workflow = await world.retire()
        world.held = list(HABITS)

        await world.run_command(workflow)

        assert world.lines == [
            ["create"],
            ["get", NOTE, "habits"],
            ["say", "Habit check-in: finished."],
        ]

    _run(scenario)


def test_the_check_in_by_itself_asks_with_low_priority_and_gives_way() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.retire()
        (pick,) = [step for step in workflow.steps if step.block == "core.ask-each"]
        assert (pick.inputs["priority"], pick.inputs["give_up_after"]) == ("low", 60)
        assert workflow.trigger.params == {"every": 1, "unit": "hours"}

        # A run that the schedule starts: one habit picked, then no answer.
        unanswered: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        world.answers = [0, unanswered]
        tick = Tick(date="2026-11-24", time="12:00")
        run = asyncio.create_task(
            world.engine._run(workflow, world.user, tick, manual=False, attempt=1)
        )
        while len([line for line in world.lines if line[0] == "choice"]) < 2:
            await asyncio.sleep(0)
        current = turns.get_current_user_interaction(4)
        assert current is not None
        assert current.priority == UserInteractionPriority.LOW

        # Any other conversation takes the turn from it.
        async with turns.user_interaction(4):
            pass
        trace = await run

        assert trace["status"] == "stopped"
        assert trace["steps"][-1]["reason"] == TOOK_OVER
        # The habit that was picked before that is marked.
        assert ["add", NOTE, "habits", "meditate"] in world.lines

    _run(scenario)


# -- from the plugin to the workflow -------------------------------------------


def test_the_user_gets_the_workflow_with_the_habits_they_had() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.retire()

        assert (workflow.recipe, workflow.enabled) == (RECIPE, True)
        today, done, pick = workflow.steps
        assert done.inputs == {
            "note": "Daily/{{ trigger.date }}.md",
            "property": "habits",
        }
        assert pick.inputs["options"] == {
            "Meditate": "meditate",
            "Exercise": "exercise",
            "Read": "read",
            # Shown as the plugin showed it, written as the user named it.
            "Take vitamin d": "take vitamin D",
        }
        assert pick.inputs["columns"] == 4
        (mark,) = pick.steps
        assert mark.inputs == {
            "note": "Daily/{{ trigger.date }}.md",
            "property": "habits",
            "value": "{{ pick.value }}",
        }
        # The plugin is gone for the user, who is told once.
        assert await world.redis.smembers(USERS_KEY) == set()
        assert await world.redis.smembers("user:4:plugins") == set()
        (told,) = world.notified
        assert "“Habit Tracker” is now a workflow, “Habit check-in”" in told
        assert "Add Habit and Remove Habit are gone" in told

        await migrations.retire_plugins(world.engine)
        assert len(await world.engine.store.list_for_user(4)) == 1
        assert len(world.notified) == 1

    _run(scenario)


def test_the_folder_the_property_and_the_number_of_habits_are_kept() -> None:
    async def scenario(world: World) -> None:
        habits = ["a", "b", "c", "d", "e", "f"]
        await world.save_old_settings("Journal/Days/", "done", habits)

        workflow = await world.retire()

        _, done, pick = workflow.steps
        assert done.inputs == {
            "note": "Journal/Days/{{ trigger.date }}.md",
            "property": "done",
        }
        assert pick.steps[0].inputs["note"] == "Journal/Days/{{ trigger.date }}.md"
        assert pick.steps[0].inputs["property"] == "done"
        assert list(pick.inputs["options"].values()) == habits
        # The plugin used as many columns as there were habits, and four
        # at least.
        assert pick.inputs["columns"] == 6
        assert world.engine.problems(workflow) == []

    _run(scenario)


def test_a_user_without_settings_gets_the_workflow_turned_off() -> None:
    async def scenario(world: World) -> None:
        await world.register()

        await migrations.retire_plugins(world.engine)

        (workflow,) = await world.engine.store.list_for_user(4)
        assert workflow.enabled is False
        # The recipe as it ships, with its examples.
        assert workflow.steps[2].inputs["options"] == {
            "Meditate": "meditate",
            "Exercise": "exercise",
            "Read": "read",
        }
        (told,) = world.notified
        assert "It is off, because the plugin had no settings" in told
        assert world.lines == []

    _run(scenario)


def test_what_a_button_says_for_a_habit() -> None:
    assert options_for(["meditate", "Read", "take vitamin D"]) == {
        "Meditate": "meditate",
        "Read": "Read",
        "Take vitamin d": "take vitamin D",
    }
    # Two habits that would read the same: the second keeps its own name.
    assert options_for(["read", "READ"]) == {"Read": "read", "READ": "READ"}
    assert options_for([]) == {}


def test_settings_without_habits_leave_the_recipe_as_it_is() -> None:
    recipe = all_recipes()[RECIPE]
    without: list[dict[str, Any] | None] = [
        None,
        {},
        {"habits": []},
        {"habits": [{"description": "x"}]},
    ]
    for saved in without:
        workflow = recipe.workflow_for(4)
        keep_settings(saved)(workflow)
        assert workflow.enabled is False
        # Where Obsidian says the daily note is.
        assert workflow.steps[1].inputs["note"] == "{{ today.path }}"

    # No folder: the daily notes are at the top of the vault.
    workflow = recipe.workflow_for(4)
    keep_settings({"daily_note_path": "", "habits": [{"name": "read"}]})(workflow)
    assert workflow.enabled is True
    assert workflow.steps[1].inputs == {
        "note": "{{ trigger.date }}.md",
        "property": "habits",
    }
