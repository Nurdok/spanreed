"""A recurring payment from end to end: the time, the task, the note.

The engine, the Todoist blocks and the Obsidian block are the real ones.
Redis is in memory, and Todoist, the vault and the user are mocks. The cases
are the ones the Recurring Payments plugin was run on before each payment
became a workflow (see ``recurring_payments_test_data``).
"""

import asyncio
import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import yaml

from spanreed.apis.obsidian import ObsidianApiTimeoutError, ObsidianPlugin
from spanreed.interaction import UserInteractionPriority, commands
from spanreed.interaction import engine as turns
from spanreed.plugin import Plugin
from spanreed.plugins.recurring_payments import (
    CANONICAL_NAME,
    PAYMENT_RECIPE,
    move,
    workflow_for_payment,
)
from spanreed.plugins.recurring_payments_test_data import (
    CASES,
    LOG,
    OLD_PLUGIN,
    TEMPLATE,
)
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.builtin import Tick
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

CONFIG_KEY = f"config:plugin:name={CANONICAL_NAME}:user_id=4"
USERS_KEY = f"config:plugin:name={CANONICAL_NAME}:users"
EVENT = Tick(date="2026-11-24", time="18:00")


def _payment(case: dict[str, Any]) -> dict[str, Any]:
    """Return a payment as the plugin saved it, for a case of the test data."""
    return {
        "todoist_label": "therapy",
        "todoist_task_template": case.get("template", TEMPLATE),
        "todoist_project_id": "P1",
        "date_format": "%Y-%m-%d",
        "recurrence_cost": case.get("cost", 150.0),
        "recurrence_info": {
            "timezone": "UTC",
            "frequency": 2,
            "week_start_day": 0,
            "week_day": 1,
            "hour": 18,
            "minute": 0,
            "second": 0,
        },
        "verify_recurrence": case.get("verify", False),
        "obsidian_log": dict(LOG) if case.get("log", LOG) else None,
    }


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    todoist: AsyncMock
    obsidian: AsyncMock
    bot: AsyncMock
    user: Any
    # The dates the task holds (``None``: there is no task), how many
    # tasks have the label, and the text around the dates in the comment.
    dates: list[str] | None = None
    tasks: int = 1
    around: list[str] = field(default_factory=lambda: ["", ""])
    # Whether the vault answers.
    vault_answers: bool = True
    answers: list[int] = field(default_factory=list)
    # What was done, as the test data holds it.
    lines: list[list[Any]] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)

    def given(self, case: dict[str, Any]) -> None:
        """Set up what a case of the test data says there was."""
        self.dates = case["dates"]
        self.tasks = case.get("tasks", 1)
        self.around = case.get("around", ["", ""])
        self.vault_answers = case.get("vault") != "timeout"
        self.answers = list(case.get("answers", []))
        self.lines = []

    async def workflow_for(self, case: dict[str, Any]) -> Workflow:
        """Return a workflow for the payment of a case, stored and on."""
        workflow = workflow_for_payment("wf_payment", 4, _payment(case), 1)
        assert self.engine.problems(workflow) == []
        await self.engine.save(workflow)
        await self.engine.store.memory(workflow.id).clear()
        return workflow

    async def run(self, workflow: Workflow) -> dict[str, Any]:
        """Run the workflow as its time of day does, and return the trace."""
        async with asyncio.timeout(3):
            return await self.engine._run(
                workflow, self.user, EVENT, manual=False, attempt=1
            )


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), TodoistPlugin()]
    )
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    todoist = AsyncMock()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.recurring_payments.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch.object(Plugin, "is_registered", async_return_true),
        # The time of day is far off: a test says when a run is.
        patch(
            "spanreed.workflows.store.StoredTriggerState.get",
            new=AsyncMock(return_value=None),
        ),
        patch("spanreed.workflows.builtin._sleep", new=asyncio.Event().wait),
        patch(
            "spanreed.apis.todoist_blocks.Todoist.for_user",
            new=AsyncMock(return_value=todoist),
        ),
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as vault,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.blocks.UserInteractionApi", new=bot),
        patch("spanreed.plugins.recurring_payments.UserInteractionApi", new=bot),
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            todoist=todoist,
            obsidian=vault,
            bot=bot,
            user=mock_user_find_by_id(4),
        )
        task = SimpleNamespace(id="T1", content="old content")

        def comment() -> Any:
            held = yaml.safe_dump({"dates": list(world.dates or [])})
            text = "---".join([world.around[0], held, world.around[1]])
            return SimpleNamespace(id="C1", content=text)

        async def with_label(label: str) -> list[Any]:
            world.lines.append(["tasks with label", label])
            return [task] * world.tasks if world.dates is not None else []

        async def first_comment(a_task: Any, create: bool = False) -> Any:
            world.lines.append(["yaml comment", a_task.id, create])
            return comment()

        async def add_task(**fields: Any) -> Any:
            world.lines.append(["add task", fields])
            return SimpleNamespace(id="T-new", content=fields["content"])

        async def add_comment(**fields: Any) -> None:
            world.lines.append(["add comment", fields["task"].id, fields["content"]])
            world.dates = yaml.safe_load(fields["content"].split("---")[1])["dates"]

        async def update_task(a_task: Any, **fields: Any) -> None:
            world.lines.append(["update task", a_task.id, fields])

        async def update_comment(a_comment: Any, **fields: Any) -> None:
            world.lines.append(["update comment", a_comment.id, fields["content"]])
            world.dates = yaml.safe_load(fields["content"].split("---")[1])["dates"]

        async def due_today(a_task: Any) -> None:
            world.lines.append(["due today", a_task.id])

        async def append(path: str, content: str, heading: Any = None) -> None:
            world.lines.append(["append", path, content])
            if not world.vault_answers:
                raise ObsidianApiTimeoutError("Obsidian did not answer.")

        async def choice(prompt: str, choices: list[Any], **how: Any) -> int:
            answer = world.answers.pop(0)
            world.lines.append(["choice", prompt, list(choices), answer])
            return answer

        async def notify(message: str, **how: Any) -> None:
            world.notified.append(message)

        todoist.get_tasks_with_label = AsyncMock(side_effect=with_label)
        todoist.get_first_comment_with_yaml = AsyncMock(side_effect=first_comment)
        todoist.add_task = AsyncMock(side_effect=add_task)
        todoist.add_comment = AsyncMock(side_effect=add_comment)
        todoist.update_task = AsyncMock(side_effect=update_task)
        todoist.update_comment = AsyncMock(side_effect=update_comment)
        todoist.set_due_date_to_today = AsyncMock(side_effect=due_today)
        vault.for_user = AsyncMock(return_value=vault)
        vault.append_to_note = AsyncMock(side_effect=append)
        bot.for_user = AsyncMock(return_value=bot)
        bot.request_user_choice = AsyncMock(side_effect=choice)
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


def _as_the_workflow_does(old: list[list[Any]]) -> list[list[Any]]:
    """Return what the plugin did, with what the workflow does on purpose.

    The step that writes finds the task again, and reads the comment before
    it names the task: the plugin had both from before. The workflow
    neither tells the user that the vault did not answer nor stops with an
    error: its run fails, which the engine deals with. And text after the
    dates in the comment keeps its place: the plugin put one more empty
    line before it each time.
    """
    expected: list[list[Any]] = []
    for line in old:
        if line[0] in ("notify", "fails"):
            continue
        if line[0] in ("add task", "update task"):
            expected.append(["tasks with label", "therapy"])
            if line[0] == "update task":
                expected.append(["yaml comment", "T1", False])
        if line == ["yaml comment", "T1", False]:
            continue
        if line[0] == "update comment":
            line = [line[0], line[1], line[2].replace("---\n\n", "---\n")]
        expected.append(line)
    return expected


def test_the_workflow_does_what_the_plugin_did_for_a_payment() -> None:
    async def scenario(world: World) -> None:
        for name, case in CASES.items():
            workflow = await world.workflow_for(case)
            world.given(case)

            run = await world.run(workflow)

            assert world.lines == _as_the_workflow_does(OLD_PLUGIN[name]), name
            assert world.answers == [], name
            old = OLD_PLUGIN[name]
            if any(line[0] in ("notify", "fails") for line in old):
                assert run["status"] == "failed", name
            else:
                assert run["status"] in ("finished", "stopped"), name

    _run(scenario)


def test_two_tasks_with_the_label_fail_the_run_with_the_plugins_words() -> None:
    async def scenario(world: World) -> None:
        case = CASES["two tasks with the label"]
        workflow = await world.workflow_for(case)
        world.given(case)

        run = await world.run(workflow)

        (plugin_said,) = [
            line[1]
            for line in OLD_PLUGIN["two tasks with the label"]
            if line[0] == "fails"
        ]
        assert run["steps"][0]["error"] == plugin_said

    _run(scenario)


def test_a_note_that_could_not_be_written_is_written_by_the_next_try() -> None:
    """Without the date going to the task twice, or the question twice."""

    async def scenario(world: World) -> None:
        case = {**CASES["asked first, yes"], "vault": "timeout"}
        workflow = await world.workflow_for(case)
        world.given(case)

        failed = await world.run(workflow)
        assert failed["status"] == "failed"
        assert failed["steps"][-1]["title"] == "Append to note"
        assert world.dates == ["2026-11-17", "2026-11-24"]

        # The engine tries the run again, from its first step.
        world.vault_answers = True
        world.lines = []
        again = await world.run(workflow)

        assert again["status"] == "finished"
        assert world.lines == [
            ["tasks with label", "therapy"],
            ["yaml comment", "T1", True],
            ["append", "Health/Therapy.md", "- 2026-11-24: session"],
        ]
        # And a run after that leaves the note alone.
        world.lines = []
        third = await world.run(workflow)
        assert third["status"] == "stopped"
        assert [line[0] for line in world.lines] == ["tasks with label", "yaml comment"]

    _run(scenario)


def test_the_question_is_asked_ahead_of_other_conversations() -> None:
    async def scenario(world: World) -> None:
        case = CASES["asked first, yes"]
        workflow = await world.workflow_for(case)
        world.given(case)
        seen: list[Any] = []

        async def choice(prompt: str, choices: list[Any], **how: Any) -> int:
            current = turns.get_current_user_interaction(4)
            seen.append(current.priority if current else None)
            return 0

        world.bot.request_user_choice = AsyncMock(side_effect=choice)
        await world.run(workflow)

        assert seen == [UserInteractionPriority.HIGH]

    _run(scenario)


# -- from the plugin to the workflows --------------------------------------------


def test_a_payment_as_a_workflow() -> None:
    workflow = workflow_for_payment("wf_1", 4, _payment({"verify": True}), 3)

    assert workflow.name == "Recurring payment: therapy"
    assert (workflow.enabled, workflow.command_hidden) == (True, True)
    assert workflow.recipe == f"{PAYMENT_RECIPE}-3"
    assert workflow.trigger.block == "core.at-time"
    # Day 1 of the plugin's week is Tuesday.
    assert workflow.trigger.params == {
        "time": "18:00",
        "on": "Tuesday",
        "timezone": "UTC",
    }
    tally, new, owed, due, log, logged = workflow.steps
    assert tally.inputs == {
        "label": "therapy",
        "date": "{{ trigger.date }}",
        "cost": 150.0,
    }
    sure, yes, write, owe = new.steps
    assert owe.inputs == owed.inputs == logged.inputs
    assert sure.inputs["question"] == (
        "You have a recurring payment of 150.0 due now (for task with label "
        "therapy).\nWould you like to add it to the list of dates?"
    )
    assert sure.inputs["priority"] == "high"
    assert write.inputs["name"].endswith(TEMPLATE)
    assert write.inputs["project"] == "P1"
    assert log.inputs["path"] == "Health/Therapy.md"

    # Without a question, and without a note.
    plain = workflow_for_payment("wf_2", 4, _payment({"log": None}), 1)
    tally, new = plain.steps
    assert [step.id for step in new.steps] == ["write"]


def test_the_user_gets_a_workflow_for_each_payment() -> None:
    async def scenario(world: World) -> None:
        allowance = {
            **_payment(
                {"template": "Allowance for {{dates}}", "cost": 0.0, "log": None}
            ),
            "todoist_label": "allowance",
        }
        saved = {"recurring_payments": [_payment({"verify": True}), allowance]}
        await world.redis.set(CONFIG_KEY, json.dumps(saved))
        await world.redis.sadd(USERS_KEY, "4")
        await world.redis.sadd("user:4:plugins", CANONICAL_NAME)

        await migrations.retire_plugins(world.engine)

        workflows = await world.engine.store.list_for_user(4)
        assert [(w.name, w.enabled, w.recipe) for w in workflows] == [
            ("Recurring payment: allowance", True, f"{PAYMENT_RECIPE}-2"),
            ("Recurring payment: therapy", True, f"{PAYMENT_RECIPE}-1"),
        ]
        for workflow in workflows:
            assert world.engine.problems(workflow) == []
            # The plugin had no command, so the workflows have none.
            assert commands.get_command(f"workflow.{workflow.id}") is None
        # The plugin is gone for the user, who is told once.
        assert await world.redis.smembers(USERS_KEY) == set()
        (told,) = world.notified
        assert "“Recurring Payments” is now workflows, one for each payment" in told
        assert "“Recurring payment: therapy” and “Recurring payment: allowance”" in told

        assert await move(world.engine, world.user) == []
        assert len(await world.engine.store.list_for_user(4)) == 2
        assert len(world.notified) == 1

    _run(scenario)


def test_a_user_without_payments_gets_nothing_and_is_not_told() -> None:
    async def scenario(world: World) -> None:
        await world.redis.sadd(USERS_KEY, "4")

        await migrations.retire_plugins(world.engine)

        assert await world.engine.store.list_for_user(4) == []
        assert world.notified == []
        assert await world.redis.smembers(USERS_KEY) == set()

    _run(scenario)


def test_the_recipe_for_a_new_payment_fits_what_a_converted_one_is() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.engine.install(world.user, all_recipes()[PAYMENT_RECIPE])
        converted = workflow_for_payment("wf_1", 4, _payment({"verify": True}), 1)

        def blocks(made: Workflow) -> list[Any]:
            return [(s.block, [i.block for i in s.steps]) for s in made.steps]

        assert world.engine.problems(workflow) == []
        assert blocks(workflow) == blocks(converted)
        assert workflow.trigger.block == converted.trigger.block
        assert workflow.command_hidden is True

    _run(scenario)
