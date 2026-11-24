"""Tests of a workflow's memory: the blocks, the store, and a run."""

import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.interaction import commands
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.blocks import (
    BlockRegistry,
    LocalMemory,
    RunInfo,
    StepContext,
)
from spanreed.workflows.builtin import ContinueIf
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.memory import (
    Forget,
    ForgetInput,
    Recall,
    RecallInput,
    Remember,
    RememberInput,
    memory_blocks,
)
from spanreed.workflows.model import Step
from spanreed.workflows.store import StoredMemory, WorkflowStore
from spanreed.workflows.testing import (
    Record,
    drain,
    fake_plugin,
    fake_storage,
    registry_of,
    workflow_of,
)


def setup_function() -> None:
    commands.reset_registry_for_tests()


def _ctx() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="run_1", manual=False),
    )


def test_the_memory_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in memory_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in memory_blocks()] == [
        "core.remember",
        "core.recall",
        "core.forget",
    ]


def test_remember_recall_and_forget() -> None:
    async def scenario() -> None:
        ctx = _ctx()
        assert isinstance(ctx.memory, LocalMemory)

        nothing = await Recall().run(ctx, RecallInput(name="asked:B1"))
        assert (nothing.found, nothing.value, nothing.at) == (False, None, "")

        await Remember().run(ctx, RememberInput(name="asked:B1"))
        await Remember().run(ctx, RememberInput(name="skipped", value=["a.pdf"]))
        asked = await Recall().run(ctx, RecallInput(name="asked:B1"))
        assert (asked.found, asked.value) == (True, True)
        assert asked.at.startswith("20")
        skipped = await Recall().run(ctx, RecallInput(name="skipped"))
        assert skipped.value == ["a.pdf"]

        # A new value takes the place of the old one.
        await Remember().run(ctx, RememberInput(name="skipped", value=[]))
        assert (await Recall().run(ctx, RecallInput(name="skipped"))).value == []

        assert (await Forget().run(ctx, ForgetInput(name="asked:B1"))).found is True
        assert (await Forget().run(ctx, ForgetInput(name="asked:B1"))).found is False
        assert (await Recall().run(ctx, RecallInput(name="asked:B1"))).found is False

    asyncio.run(scenario())


def test_recall_says_how_long_ago_something_was_kept() -> None:
    """For doing something at most every so often."""

    async def scenario() -> None:
        ctx = _ctx()
        await Remember().run(ctx, RememberInput(name="asked"))
        kept = await ctx.memory.get("asked")
        assert kept is not None
        then = datetime.datetime.fromisoformat(kept.at)

        with patch("spanreed.workflows.memory._now") as now:
            now.return_value = then + datetime.timedelta(hours=73, minutes=30)
            later = await Recall().run(ctx, RecallInput(name="asked"))
        assert later.age_hours == 73.5
        assert (await Recall().run(ctx, RecallInput(name="never"))).age_hours == 0

    asyncio.run(scenario())


def test_the_store_keeps_a_memory_for_each_workflow() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        one, other = store.memory("wf_1"), store.memory("wf_2")
        assert isinstance(one, StoredMemory)

        await one.set("asked:B1")
        await one.set("skipped", {"files": ["a.pdf"], "count": 1})
        await other.set("asked:B1", "theirs")

        kept = await one.get("skipped")
        assert kept is not None
        assert kept.value == {"files": ["a.pdf"], "count": 1}
        assert kept.at.endswith("+00:00")
        mine = await one.get("asked:B1")
        theirs = await other.get("asked:B1")
        assert mine is not None and theirs is not None
        assert (mine.value, theirs.value) == (True, "theirs")
        assert await one.get("nothing") is None
        assert await one.names() == ["asked:B1", "skipped"]

        assert await one.forget("skipped") is True
        assert await one.forget("skipped") is False
        await one.clear()
        assert await one.names() == []
        assert await other.names() == ["asked:B1"]

        # Something a plugin remembered keeps the time it was true at.
        await one.set("asked", True, at="2026-11-20T09:00:00")
        carried = await one.get("asked")
        assert carried is not None and carried.at == "2026-11-20T09:00:00+00:00"
        await one.set("asked", True, at="not a time")
        again = await one.get("asked")
        assert again is not None and again.at.startswith("20")
        assert again.at != carried.at

        # Deleting a workflow deletes what it remembers.
        workflow = workflow_of(id="wf_2")
        await store.save(workflow)
        await store.delete(workflow)
        assert await other.names() == []

    with fake_storage():
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_remembers_for_the_runs_after_it(_: AsyncMock) -> None:
    """Do something once for each thing, however often it comes up."""
    record = Record()
    workflow = workflow_of(
        Step(
            id="seen",
            block="core.recall",
            inputs={"name": "told:{{ trigger.value }}"},
        ),
        Step(
            id="new",
            block="core.continue-if",
            inputs={"condition": "{{ not seen.found }}"},
        ),
        Step(id="tell", block="fake.record", inputs={"text": "{{ trigger.value }}"}),
        Step(
            id="keep",
            block="core.remember",
            inputs={"name": "told:{{ trigger.value }}", "value": "{{ tell.length }}"},
        ),
        events=["dune", "emma", "dune", "dune"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = WorkflowEngine(registry_of(record, Remember(), Recall(), ContinueIf()))
        await engine.save(workflow)
        await drain(engine, workflow)
        kept = await engine.store.memory(workflow.id).get("told:dune")
        assert kept is not None and kept.value == 4
        return list(reversed(await engine.store.recent_runs(workflow.id, 10)))

    with fake_storage():
        runs = asyncio.run(scenario())

    assert record.texts == ["dune", "emma"]
    assert [run["status"] for run in runs] == [
        "finished",
        "finished",
        "stopped",
        "stopped",
    ]
    assert runs[2]["steps"][0]["outputs"]["found"] is True
