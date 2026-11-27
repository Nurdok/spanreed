"""Timekiller from end to end: the pick, the four activities, the vault.

The engine, the Timekiller blocks, the asking blocks and the Obsidian blocks
are the real ones. Redis is in memory, and the vault and the user are mocks.
The cases are the ones the Timekiller plugin was run on before it became
workflows (see ``timekiller_test_data``).
"""

import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.obsidian import DailyNote, ObsidianPlugin
from spanreed.interaction import UserInteractionPriority, commands
from spanreed.interaction import engine as turns
from spanreed.plugin import Plugin
from spanreed.plugins.timekiller import (
    BOOKS_RECIPE,
    JOURNAL_RECIPE,
    KILL_TIME_RECIPE,
    LAST_ASKED_BOOKS_KEY,
    MOOD_RECIPE,
    SCANS_RECIPE,
    SKIPPED_SCANS_KEY,
    TimekillerPlugin,
    convert_to_workflows,
)
from spanreed.plugins.timekiller_blocks import PROMPTS, timekiller_blocks
from spanreed.plugins.timekiller_test_data import CASES, OLD_PLUGIN
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
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow

CONFIG_KEY = "config:plugin:name=timekiller:user_id=4"
USERS_KEY = "config:plugin:name=timekiller:users"
NOW = datetime.datetime(2026, 11, 24, 10, 0, tzinfo=datetime.timezone.utc)
TODAY = "2026-11-24"
YESTERDAY = "2026-11-23"
NOTE = f"Daily/{TODAY}.md"
FEELINGS = "What are you feeling right now?"

SCANS = [
    "Assets/scans/2026-11-20 Scan 1.pdf",
    "Assets/scans/2026-10-01 Contract.pdf",
    "Assets/scans/2026-11-21 Scan 2.jpg",
    "Assets/scans/2026-11-22 Scan 3.pdf",
]
BOOKS = [["Books/Dune.md", "Dune"], ["Books/Emma.md", "Emma"]]
# The recipe that does what each kind of case of the test data ran.
RECIPE_OF = {
    "journal": JOURNAL_RECIPE,
    "mood": MOOD_RECIPE,
    "books": BOOKS_RECIPE,
    "scans": SCANS_RECIPE,
}


def _dated(text: Any) -> Any:
    """Return a text of the test data with the dates of the tests in it."""
    if isinstance(text, str):
        return text.replace("<today>", TODAY).replace("<yesterday>", YESTERDAY)
    return text


def _step(workflow: Workflow, step_id: str) -> Any:
    """Return the step of a workflow that has an id, from its top steps."""
    (step,) = [step for step in workflow.steps if step.id == step_id]
    return step


def _flat(choices: list[Any]) -> list[str]:
    """Return the choices of a question in the order they are read."""
    flat: list[str] = []
    for choice in choices:
        flat.extend(choice if isinstance(choice, list) else [choice])
    return flat


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    obsidian: AsyncMock
    bot: AsyncMock
    user: Any
    workflows: dict[str, Workflow] = field(default_factory=dict)
    # What the notes hold, by ``path|property``.
    properties: dict[str, Any] = field(default_factory=dict)
    books: list[list[str]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    taken: list[str] = field(default_factory=list)
    # What "at random" gives for the activity. The prompts come in order.
    pick: str = ""
    prompts_picked: int = 0
    # The user's answers: the text of a choice, or what is written.
    answers: list[str] = field(default_factory=list)
    # What was done, as the test data holds it, with the text of an answer
    # in place of its place.
    lines: list[list[Any]] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)

    async def save_old_state(
        self,
        heading: str | None = None,
        last_asked: str | None = None,
        skipped: list[str] | None = None,
    ) -> None:
        """Save what the plugin had for the user."""
        await self.redis.set(CONFIG_KEY, json.dumps({"journal_heading": heading}))
        if last_asked is not None:
            await TimekillerPlugin.set_user_data(
                self.user, LAST_ASKED_BOOKS_KEY, last_asked
            )
        if skipped is not None:
            await TimekillerPlugin.set_user_data(
                self.user, SKIPPED_SCANS_KEY, json.dumps(skipped)
            )

    async def convert(self) -> dict[str, Workflow]:
        """Give the user the workflows, as the conversion at startup does."""
        await convert_to_workflows(self.engine, self.user)
        self.workflows = {
            str(workflow.recipe): workflow
            for workflow in await self.engine.store.list_for_user(4)
        }
        return self.workflows

    async def given(self, case: dict[str, Any], old: list[list[Any]]) -> None:
        """Set up what a case of the test data says there was."""
        self.properties = {
            _dated(key): value for key, value in case.get("properties", {}).items()
        }
        self.books = case.get("books", BOOKS)
        self.files = list(case.get("files", SCANS))
        self.taken = list(case.get("taken", []))
        self.pick = case.get("pick", "")
        self.prompts_picked = 0
        self.lines = []
        # The answers by their text, so that a choice whose place moved
        # is still the same choice.
        self.answers = [
            _flat(line[2])[line[4]] if line[0] == "choice" else line[2]
            for line in old
            if line[0] in ("choice", "input")
        ]
        journal = self.workflows[JOURNAL_RECIPE]
        _step(journal, "journal").inputs["heading"] = case.get("heading")
        await self.engine.save(journal)
        memory = self.engine.store.memory(self.workflows[KILL_TIME_RECIPE].id)
        await memory.forget(LAST_ASKED_BOOKS_KEY)
        for name, value in case.get("memory", {}).items():
            await memory.set(name, True, at=value)
        scans = self.engine.store.memory(self.workflows[SCANS_RECIPE].id)
        for name in await scans.shown():
            await scans.forget(name)

    async def run(self, case: dict[str, Any]) -> dict[str, Any]:
        """Run what a case ran, and return the trace of that run."""
        what = case["run"]
        if what == "push":
            workflow = self.workflows[KILL_TIME_RECIPE]
            async with asyncio.timeout(3):
                return await self.engine._run(
                    workflow,
                    self.user,
                    Tick(date=TODAY, time="10:00"),
                    manual=False,
                    attempt=1,
                )
        recipe = KILL_TIME_RECIPE if what == "by hand" else RECIPE_OF[what]
        workflow = self.workflows[recipe]
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
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), TimekillerPlugin()]
    )
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
        patch("spanreed.workflows.asking._now", return_value=NOW),
        # How long ago something was remembered is counted from the same
        # moment, not from the day the test runs on.
        patch("spanreed.workflows.memory._now", return_value=NOW),
        patch("spanreed.plugins.timekiller_blocks._today", return_value=NOW.date()),
        patch("spanreed.plugins.timekiller_blocks.ObsidianApi", autospec=True) as vault,
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
            bot=bot,
            user=mock_user_find_by_id(4),
        )

        async def create(date: datetime.date) -> DailyNote:
            world.lines.append(["create"])
            return await daily_note_in_daily_folder(date)

        async def get(note: str, name: str) -> Any:
            world.lines.append(["get", note, name])
            return world.properties.get(f"{note}|{name}")

        async def set_(note: str, name: str, value: Any) -> None:
            world.lines.append(["set", note, name, value])
            world.properties[f"{note}|{name}"] = value

        async def append(note: str, content: str, heading: Any = None) -> None:
            world.lines.append(["append", note, content, heading])

        async def query(text: str) -> list[Any]:
            world.lines.append(["query"])
            return [
                SimpleNamespace(file={"path": path}, title=title)
                for path, title in world.books
            ]

        async def list_dir(folder: str) -> list[str]:
            world.lines.append(["list", folder])
            return list(world.files)

        async def read(path: str) -> bytes:
            world.lines.append(["read", path])
            return b"%PDF"

        async def delete(path: str) -> None:
            world.lines.append(["delete", path])
            world.files.remove(path)

        async def move(source: str, target: str) -> None:
            world.lines.append(["move", source, target])
            if target in world.taken:
                raise FileExistsError(target)
            world.files.remove(source)

        async def choice(prompt: str, choices: list[Any], **how: Any) -> int:
            flat = _flat(choices)
            answer = world.answers.pop(0)
            world.lines.append(["choice", prompt, flat, how.get("columns", 1), answer])
            return flat.index(answer)

        async def text(prompt: str) -> str:
            answer = world.answers.pop(0)
            world.lines.append(["input", prompt, answer])
            return answer

        async def say(message: str, **how: Any) -> None:
            world.lines.append(["say", message])

        async def send_file(name: str, data: bytes) -> None:
            world.lines.append(["file", name])

        async def notify(message: str, **how: Any) -> None:
            world.notified.append(message)

        real_set = workflow_store.StoredMemory.set

        async def remember(
            memory: Any, name: str, value: Any = True, at: Any = None
        ) -> None:
            if name != workflow_store.MOVED_MARKER and at is None:
                world.lines.append(["remember", name])
            await real_set(memory, name, value, at)

        def one_of(names: list[str]) -> str:
            world.lines.append(["on offer", list(names)])
            return world.pick

        def next_prompt(prompts: list[str]) -> str:
            world.prompts_picked += 1
            return prompts[world.prompts_picked - 1]

        vault.for_user = AsyncMock(return_value=vault)
        vault.create_daily_note = AsyncMock(side_effect=create)
        vault.find_daily_note = AsyncMock(side_effect=daily_note_in_daily_folder)
        vault.get_property = AsyncMock(side_effect=get)
        vault.set_value_of_property = AsyncMock(side_effect=set_)
        vault.append_to_note = AsyncMock(side_effect=append)
        vault.query_dataview = AsyncMock(side_effect=query)
        vault.list_dir = AsyncMock(side_effect=list_dir)
        vault.read_binary_file = AsyncMock(side_effect=read)
        vault.delete_file = AsyncMock(side_effect=delete)
        vault.move_file = AsyncMock(side_effect=move)
        bot.for_user = AsyncMock(return_value=bot)
        bot.request_user_choice = AsyncMock(side_effect=choice)
        bot.request_user_input = AsyncMock(side_effect=text)
        bot.send_message = AsyncMock(side_effect=say)
        bot.send_document = AsyncMock(side_effect=send_file)
        bot.notify = AsyncMock(side_effect=notify)
        with (
            patch.object(workflow_store.StoredMemory, "set", new=remember),
            patch("spanreed.workflows.calling._choice", new=one_of),
            patch("spanreed.plugins.timekiller_blocks._choice", new=next_prompt),
        ):
            yield world


def _run(scenario: Any) -> None:
    with _world() as world:

        async def run() -> None:
            try:
                await scenario(world)
            finally:
                await world.engine.stop()

        asyncio.run(run())


SUMMARY = {
    "books": "Books: finished.",
    "scans": "Scan Processing: finished.",
}
SKIPPED_OLD = "Use “Clear skipped scans” to reset."
SKIPPED_NEW = "To be asked again, make the workflow forget it on its page."


def _as_the_workflows_do(case: dict[str, Any], old: list[list[Any]]) -> list[list[Any]]:
    """Return what the plugin did, with what the workflows do on purpose.

    What differs:

    - A feeling that was picked is not offered again, and the list has
      "stressed" once: the plugin had it twice.
    - That the books were asked about is remembered by "Kill time", when it
      ran "Books", and not by the check itself.
    - A scan that is skipped is remembered by itself, and the message says
      where to undo that.
    - A run by hand that asked nothing says that it finished.
    """
    expected: list[list[Any]] = []
    felt: list[str] = []
    ran_books = False
    for line in old:
        line = [_dated(item) for item in line]
        if line[0] == "choice":
            choices = _flat(line[2])
            answer = choices[line[4]]
            if line[1] == FEELINGS:
                choices = [c for c in dict.fromkeys(choices) if c not in felt]
                felt.append(answer)
            line = ["choice", line[1], choices, line[3], answer]
        elif line[0] == "remember" and line[1] == LAST_ASKED_BOOKS_KEY:
            continue
        elif line[0] == "remember" and line[1] == SKIPPED_SCANS_KEY:
            skipped = next(done[1] for done in reversed(expected) if done[0] == "read")
            line = ["remember", f"skipped:{skipped}"]
        elif line[0] == "say":
            line = ["say", line[1].replace(SKIPPED_OLD, SKIPPED_NEW)]
        elif line[0] == "query" and case["run"] in ("push", "by hand"):
            ran_books = True
        expected.append(line)
    if ran_books:
        expected.append(["remember", LAST_ASKED_BOOKS_KEY])
    asked = any(line[0] in ("choice", "say", "file") for line in expected)
    if not asked and case["run"] in SUMMARY:
        expected.append(["say", SUMMARY[case["run"]]])
    return expected


def test_the_workflows_do_what_the_plugin_did() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_state()
        workflows = await world.convert()
        for workflow in workflows.values():
            assert world.engine.problems(workflow) == [], workflow.name

        for name, case in CASES.items():
            old = OLD_PLUGIN[name]
            await world.given(case, old)
            for time in range(case.get("times", 1)):
                if time:
                    world.lines.append(["again"])
                run = await world.run(case)
                assert run["status"] in ("finished", "stopped"), name

            assert world.lines == _as_the_workflows_do(case, old), name
            assert world.answers == [], name

    _run(scenario)


def test_the_feelings_are_the_ones_the_plugin_offered() -> None:
    async def scenario(world: World) -> None:
        await world.convert()
        case = CASES["mood: no feelings"]
        old = OLD_PLUGIN["mood: no feelings"]
        await world.given(case, old)

        await world.run(case)

        (offered,) = [line[2] for line in world.lines if line[1:2] == [FEELINGS]]
        (before,) = [line[2] for line in old if line[1:2] == [FEELINGS]]
        assert offered == list(dict.fromkeys(before))
        assert len(offered) == 33 and offered[-1] == "Done"

    _run(scenario)


def test_kill_time_by_itself_gives_way_and_asks_with_normal_priority() -> None:
    async def scenario(world: World) -> None:
        workflows = await world.convert()
        kill_time = workflows[KILL_TIME_RECIPE]
        assert kill_time.trigger.block == "core.random-schedule"
        assert kill_time.trigger.params == {
            "at_least": 1,
            "at_most": 2,
            "unit": "hours",
            "only_from": "08:00",
            "only_until": "22:00",
        }
        await world.given({"run": "push", "pick": "Journaling Prompt"}, [])
        unanswered: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        asked = asyncio.Event()

        async def wait(prompt: str, choices: list[Any], **how: Any) -> int:
            asked.set()
            return await unanswered

        world.bot.request_user_choice = AsyncMock(side_effect=wait)
        run = asyncio.create_task(world.run({"run": "push"}))
        async with asyncio.timeout(3):
            await asked.wait()
        current = turns.get_current_user_interaction(4)
        assert current is not None
        assert current.priority == UserInteractionPriority.NORMAL

        # A command the user starts takes the turn: the push is dropped.
        async with turns.user_interaction(4, priority=UserInteractionPriority.HIGH):
            pass
        trace = await run

        assert trace["status"] == "stopped"
        journal = workflows[JOURNAL_RECIPE]
        (part,) = await world.engine.store.recent_runs(journal.id, 1)
        assert (part["status"], part["called_by"]) == ("stopped", "Kill time")
        world.obsidian.append_to_note.assert_not_awaited()

    _run(scenario)


def test_a_workflow_that_is_off_is_not_picked() -> None:
    async def scenario(world: World) -> None:
        workflows = await world.convert()
        scans = workflows[SCANS_RECIPE]
        scans.enabled = False
        await world.engine.save(scans)
        await world.given({"run": "push", "pick": "Journaling Prompt"}, [])
        world.answers = ["Cancel"]

        await world.run({"run": "push"})

        (offer,) = [line[1] for line in world.lines if line[0] == "on offer"]
        assert offer == ["Journaling Prompt", "Books", "Mood"]

    _run(scenario)


# -- the blocks ----------------------------------------------------------------


def test_the_plugin_is_a_pack_of_blocks() -> None:
    Plugin.reset_registry()
    plugin = TimekillerPlugin()

    assert plugin.name() == "Timekiller"
    assert plugin.canonical_name() == "timekiller"
    assert plugin.has_user_config() is False
    assert plugin.get_prerequisites() == [ObsidianPlugin]
    assert [block.id for block in plugin.blocks()] == [
        "timekiller.journal",
        "timekiller.reading-check-in",
        "timekiller.process-scan",
    ]
    assert all(block.converses for block in timekiller_blocks())  # type: ignore[attr-defined]
    assert len(PROMPTS) == 24


def test_a_date_that_is_not_one_is_asked_for_again() -> None:
    """The plugin failed on it."""

    async def scenario(world: World) -> None:
        await world.convert()
        await world.given({"run": "books", "books": BOOKS[:1]}, [])
        world.answers = ["Finished!", "Other (specify)", "last week", "2026-11-17"]

        await world.run({"run": "books"})

        assert ["set", "Books/Dune.md", "finish-date", "2026-11-17"] in world.lines

    _run(scenario)


def test_a_scan_is_renamed_in_the_folder_of_the_step() -> None:
    """The plugin wrote the folder into the new name by itself."""

    async def scenario(world: World) -> None:
        workflows = await world.convert()
        scans = workflows[SCANS_RECIPE]
        scans.steps[0].inputs["folder"] = "Inbox/Scans/"
        await world.engine.save(scans)
        await world.given(
            {"run": "scans", "files": ["Inbox/Scans/2026-11-20 Scan 1.pdf"]}, []
        )
        world.answers = ["Rename", "Yes (keep)", "Bill", "Yes"]

        run = await world.run({"run": "scans"})

        assert ["list", "Inbox/Scans"] in world.lines
        assert [
            "move",
            "Inbox/Scans/2026-11-20 Scan 1.pdf",
            "Inbox/Scans/2026-11-20 Bill.pdf",
        ] in world.lines
        assert run["steps"][0]["outputs"] == {
            "outcome": "renamed",
            "path": "Inbox/Scans/2026-11-20 Scan 1.pdf",
            "new_path": "Inbox/Scans/2026-11-20 Bill.pdf",
        }

    _run(scenario)


# -- from the plugin to the workflows --------------------------------------------


def test_the_user_gets_five_workflows_with_what_the_plugin_knew() -> None:
    async def scenario(world: World) -> None:
        four_days_ago = "2026-11-20T09:00:00"
        await world.save_old_state(
            heading="Journal",
            last_asked=four_days_ago,
            skipped=[SCANS[0], "Assets/scans/2026-09-09 Scan 9.pdf"],
        )

        workflows = await world.convert()

        assert {workflow.name for workflow in workflows.values()} == {
            "Kill time",
            "Journaling Prompt",
            "Mood",
            "Books",
            "Scan Processing",
        }
        assert all(workflow.enabled for workflow in workflows.values())
        # One command, as before: the one that lets the user pick.
        shown = {
            workflow.name: commands.get_command(f"workflow.{workflow.id}") is not None
            for workflow in workflows.values()
        }
        assert shown == {
            "Kill time": True,
            "Journaling Prompt": False,
            "Mood": False,
            "Books": False,
            "Scan Processing": False,
        }
        journal = _step(workflows[JOURNAL_RECIPE], "journal")
        assert journal.inputs["heading"] == "Journal"
        assert journal.inputs["prompts"] == PROMPTS

        kill_time = world.engine.store.memory(workflows[KILL_TIME_RECIPE].id)
        last = await kill_time.get(LAST_ASKED_BOOKS_KEY)
        # With the time it was true at, not the time of the conversion.
        assert last is not None and last.at == "2026-11-20T09:00:00+00:00"
        scans = world.engine.store.memory(workflows[SCANS_RECIPE].id)
        assert await scans.shown() == [
            "skipped:Assets/scans/2026-09-09 Scan 9.pdf",
            f"skipped:{SCANS[0]}",
        ]

        # Doing it again installs nothing.
        assert await convert_to_workflows(world.engine, world.user) == []
        assert len(await world.engine.store.list_for_user(4)) == 5

    _run(scenario)


def test_a_skipped_scan_stays_skipped_until_the_workflow_forgets_it() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_state(skipped=[SCANS[0]])
        workflows = await world.convert()
        case = {"run": "scans"}
        world.files = list(SCANS)
        world.answers = ["Cancel"]

        await world.run(case)
        assert ["read", SCANS[3]] in world.lines

        # What "Clear skipped scans" did: the page makes the workflow forget.
        memory = world.engine.store.memory(workflows[SCANS_RECIPE].id)
        assert await memory.forget(f"skipped:{SCANS[0]}") is True
        world.lines, world.answers = [], ["Cancel"]
        await world.run(case)
        assert ["read", SCANS[0]] in world.lines

    _run(scenario)


def test_a_conversion_that_stopped_halfway_is_finished_the_next_time() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_state(last_asked="2026-11-23T09:00:00")

        with patch(
            "spanreed.plugins.timekiller._carry_over",
            side_effect=RuntimeError("Redis went away"),
        ):
            try:
                await convert_to_workflows(world.engine, world.user)
            except RuntimeError:
                pass
        workflows = {
            str(w.recipe): w for w in await world.engine.store.list_for_user(4)
        }
        assert len(workflows) == 5
        # Off until it knows when the books were asked about.
        assert workflows[KILL_TIME_RECIPE].enabled is False

        workflows = await world.convert()
        assert workflows[KILL_TIME_RECIPE].enabled is True
        memory = world.engine.store.memory(workflows[KILL_TIME_RECIPE].id)
        assert await memory.get(LAST_ASKED_BOOKS_KEY) is not None

    _run(scenario)


def test_the_user_is_told_once_what_became_of_the_plugin() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_state()
        await world.redis.sadd(USERS_KEY, "4")

        await migrations.convert_plugins(world.engine)
        await migrations.convert_plugins(world.engine)

        (told,) = world.notified
        assert "Timekiller now does its work through workflows:" in told
        for name in (
            "Kill time",
            "Journaling Prompt",
            "Mood",
            "Books",
            "Scan Processing",
        ):
            assert f"“{name}”" in told
        assert "Clear skipped scans is on the page of “Scan Processing”" in told
        assert len(await world.engine.store.list_for_user(4)) == 5

    _run(scenario)
