"""Tests of a run that goes on after a restart, from the place it kept."""

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.interaction import commands
from spanreed.interaction import engine as turns
from spanreed.test_utils import FakeRedis, mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.asking import asking_blocks
from spanreed.workflows.blocks import Action, NoInput, StepContext
from spanreed.workflows.builtin import RandomSchedule, Schedule, Tick, builtin_blocks
from spanreed.workflows.calling import calling_blocks
from spanreed.workflows.engine import CANCELLED, WorkflowEngine, digest
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.testing import (
    Asking,
    Event,
    Events,
    Polled,
    Record,
    TextInput,
    TextOutput,
    fake_storage,
    registry_of,
    workflow_of,
)

PLACES = "workflows:workflow:wf_1:places"


def setup_function() -> None:
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()


class Hold(Action[TextInput, TextOutput]):
    """A step that does not end while it is closed."""

    id = "fake.hold"
    title = "Fake hold"

    def __init__(self) -> None:
        """Create the block, closed, with nothing started."""
        self.open = False
        self.started: list[str] = []

    async def run(self, ctx: StepContext, inputs: TextInput) -> TextOutput:
        """Note the text, and wait for good unless the block is open."""
        self.started.append(inputs.text)
        if not self.open:
            await asyncio.Event().wait()
        return TextOutput(text=inputs.text, length=len(inputs.text))


@dataclass
class Loaded:
    """What ``Load`` gives: something that JSON does not have."""

    data: bytes = b""
    names: list[str] = field(default_factory=list)


class Load(Action[NoInput, Loaded]):
    """A step that gives bytes."""

    id = "fake.load"
    title = "Fake load"

    def __init__(self) -> None:
        """Create the block with no call counted."""
        self.calls = 0

    async def run(self, ctx: StepContext, inputs: NoInput) -> Loaded:
        """Return some bytes."""
        self.calls += 1
        return Loaded(data=b"\x00\x01", names=["a"])


def _step(step_id: str, block: str, text: str | None = None, **more: Any) -> Step:
    inputs = {} if text is None else {"text": text}
    return Step(id=step_id, block=block, inputs={**inputs, **more})


class World:
    """Two processes, one after the other, on the same Redis."""

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.record = Record()
        self.hold = Hold()
        self.load = Load()
        self.asking = Asking()
        self.engine = self._engine()

    def _engine(self) -> WorkflowEngine:
        registry = registry_of(self.record, self.hold, self.load, self.asking)
        for block in (*builtin_blocks(), *asking_blocks(), *calling_blocks()):
            registry.register(block, registry.plugin_of("fake.record"))
        return WorkflowEngine(registry)

    async def until(self, predicate: Any) -> None:
        """Wait until the runs made something true."""
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0)

    async def places(self, workflow: Workflow) -> dict[str, dict[str, Any]]:
        """Return what is kept for the runs of a workflow that are not over."""
        return await self.engine.store.places(workflow.id)

    async def runs(self, workflow: Workflow) -> list[dict[str, Any]]:
        """Return the traces of a workflow's runs, oldest first."""
        return list(reversed(await self.engine.store.recent_runs(workflow.id, 100)))

    async def restart(self, kill: bool = False, tasks: Any = ()) -> None:
        """Stop Spanreed and start it again, with the workflows of user 4.

        ``kill`` ends it as a signal that cannot be caught does: nothing
        gets to write anything on the way out. ``tasks`` are the runs that
        the user started, which are not the engine's to stop.
        """
        state = copy.deepcopy(vars(self.redis))
        if not kill:
            await self.engine.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if kill:
            # Stop the tasks of the test's process, and put back what a
            # process that was killed would have left.
            for task in list(self.engine._sources.values()):
                task.cancel()
            await asyncio.gather(*self.engine._sources.values(), return_exceptions=True)
            vars(self.redis).update(state)
        commands.reset_registry_for_tests()
        turns.reset_state_for_tests()
        self.hold.open = True
        self.engine = self._engine()
        await self.engine.start([4])


def _run(scenario: Any) -> None:
    async def main(redis: FakeRedis) -> None:
        world = World(redis)
        try:
            await scenario(world)
        finally:
            await world.engine.stop()

    with fake_storage() as redis, patch(
        "spanreed.workflows.engine._sleep", new=AsyncMock()
    ):
        asyncio.run(main(redis))


def _three_steps(events: list[str] | None = None, **fields: Any) -> Workflow:
    return workflow_of(
        _step("one", "fake.record", "{{ trigger.value }} one"),
        _step("two", "fake.hold", "{{ one.text }}, two"),
        _step("three", "fake.record", "{{ two.text }}, three ({{ two.length }})"),
        events=["monday"] if events is None else events,
        **fields,
    )


# -- the place ---------------------------------------------------------------


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_keeps_its_place_after_each_step_and_drops_it_at_its_end(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)

        ((run_id, place),) = (await world.places(workflow)).items()
        assert place["workflow"] == digest(workflow)
        assert place["manual"] is False
        assert place["event"] == {"value": "monday"}
        assert place["event_is_plain"] is True
        # The step that is done, as the trace has it, and what it gave.
        assert [entry["id"] for entry in place["steps"]] == ["one"]
        assert place["outputs"] == {"one": {"text": "monday one", "length": 10}}
        assert place["picked_up"] == 0

        # The run ends: nothing is left of its place.
        world.hold.open = True
        for task in list(world.engine._sources.values()):
            task.cancel()
        await world.engine.save(workflow)
        await world.until(lambda: len(world.record.texts) >= 3)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_that_fails_or_is_ended_by_a_step_leaves_no_place(
    mock_bot: AsyncMock,
) -> None:
    failing = workflow_of(
        _step("one", "fake.record", "one"),
        _step("two", "fake.record", "two"),
        events=["monday"],
    )
    ending = workflow_of(
        _step("one", "fake.record", "kept"),
        Step(id="stop", block="core.continue-if", inputs={"condition": False}),
        id="wf_2",
        events=["monday"],
    )

    async def scenario(world: World) -> None:
        world.record.failures = [None, RuntimeError("boom")] * world.engine.MAX_ATTEMPTS
        await world.engine.save(failing)
        # Every try failed at the second step, and the user was told.
        await world.until(lambda: mock_bot.notify.await_count)
        assert len(await world.runs(failing)) == world.engine.MAX_ATTEMPTS
        assert await world.places(failing) == {}

        await world.engine.save(ending)
        await world.until(lambda: world.record.texts[-1] == "kept")
        await world.until(lambda: world.engine._sources[ending.id].done())
        assert await world.places(ending) == {}

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_is_turned_off_in_the_middle_of_a_run_keeps_no_place(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        assert await world.places(workflow)

        workflow.enabled = False
        await world.engine.save(workflow)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

        (run,) = await world.runs(workflow)
        assert run["status"] == CANCELLED

    _run(scenario)


# -- going on ----------------------------------------------------------------


def _goes_on(kill: bool) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        ((run_id, place),) = (await world.places(workflow)).items()

        await world.restart(kill=kill)
        await world.until(lambda: len(world.record.texts) == 2)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

        # The first step did not run again. The second started over.
        assert world.record.texts == ["monday one", "monday one, two, three (15)"]
        assert world.hold.started == ["monday one, two"] * 2
        (run,) = await world.runs(workflow)
        assert run["id"] == run_id
        assert run["started_at"] == place["started_at"]
        assert run["status"] == "finished"
        assert run["picked_up"] == 1
        assert [entry["id"] for entry in run["steps"]] == ["one", "two", "three"]
        assert run["steps"][0] == place["steps"][0]

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_goes_on_after_spanreed_was_stopped(mock_bot: AsyncMock) -> None:
    _goes_on(kill=False)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_goes_on_after_spanreed_was_killed(mock_bot: AsyncMock) -> None:
    _goes_on(kill=True)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_that_was_stopped_twice_goes_on_twice(mock_bot: AsyncMock) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        await world.restart()
        # Still closed the second time.
        world.hold.open = False
        await world.until(lambda: len(world.hold.started) == 2)
        ((_, place),) = (await world.places(workflow)).items()
        assert place["picked_up"] == 1

        await world.restart()
        await world.until(lambda: len(world.record.texts) == 2)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

        (run,) = await world.runs(workflow)
        assert run["picked_up"] == 2
        assert world.record.texts[0] == "monday one"

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_for_another_event_does_not_go_on_from_the_place(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        ((old_id, _),) = (await world.places(workflow)).items()
        # After the restart the trigger has another event.
        workflow.trigger.params["events"] = ["tuesday"]
        await world.engine.store.save(workflow)

        await world.restart(kill=True)
        await world.until(lambda: len(world.record.texts) == 3)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

        assert world.record.texts[1:] == ["tuesday one", "tuesday one, two, three (16)"]
        (run,) = await world.runs(workflow)
        assert run["id"] != old_id
        assert "picked_up" not in run

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_changed_does_not_go_on_from_the_place(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        # Changed behind the back of the run, as by another process.
        workflow.steps[2].inputs["text"] = "another third step"
        await world.engine.store.save(workflow)

        await world.restart(kill=True)
        await world.until(lambda: len(world.record.texts) == 3)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

        assert world.record.texts[1:] == ["monday one", "another third step"]
        (run,) = await world.runs(workflow)
        assert "picked_up" not in run

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_starts_over_if_what_a_step_gave_cannot_be_made_again(
    mock_bot: AsyncMock,
) -> None:
    workflow = workflow_of(
        _step("one", "fake.record", "{{ trigger.value }} one"),
        Step(id="load", block="fake.load"),
        _step("two", "fake.hold", "{{ load.names[0] }}"),
        _step("three", "fake.record", "{{ load.data | length }} bytes"),
        events=["monday"],
    )

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        ((run_id, place),) = (await world.places(workflow)).items()
        # From the step that gave bytes, the place is the start of the run.
        assert (place["steps"], place["outputs"]) == ([], {})

        await world.restart(kill=True)
        await world.until(lambda: len(world.record.texts) == 3)

        assert world.record.texts == ["monday one", "monday one", "2 bytes"]
        assert world.load.calls == 2
        (run,) = await world.runs(workflow)
        # It is the same run all the same.
        assert (run["id"], run["picked_up"]) == (run_id, 1)
        assert len(run["steps"]) == 4

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_that_holds_steps_starts_over(mock_bot: AsyncMock) -> None:
    loop = Step(
        id="each",
        block="core.for-each",
        inputs={"items": ["a", "b"]},
        steps=[
            _step("inside", "fake.record", "{{ each.item }}"),
            _step("held", "fake.hold", "{{ each.item }}"),
        ],
    )
    workflow = workflow_of(
        _step("one", "fake.record", "before"),
        loop,
        _step("after", "fake.record", "{{ each.count }} items"),
        events=["monday"],
    )

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        ((_, place),) = (await world.places(workflow)).items()
        assert [entry["id"] for entry in place["steps"]] == ["one"]

        await world.restart()
        await world.until(lambda: len(world.record.texts) == 5)

        # The loop went through its first item again.
        assert world.record.texts == ["before", "a", "a", "b", "2 items"]

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_place_that_would_be_too_large_is_not_kept(mock_bot: AsyncMock) -> None:
    workflow = workflow_of(
        _step("one", "fake.record", "{{ 'x' * 3000 }}"),
        _step("two", "fake.hold", "small"),
        _step("three", "fake.record", "{{ one.length }} long"),
        events=["monday"],
    )

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        await world.until(lambda: world.hold.started)
        # There was a place before the first step, and none after it.
        assert await world.places(workflow) == {}

        await world.restart(kill=True)
        await world.until(lambda: len(world.record.texts) == 3)

        # The run started over, as a new run.
        assert world.record.texts[2] == "3000 long"
        (run,) = await world.runs(workflow)
        assert "picked_up" not in run

    with patch("spanreed.workflows.engine._Place.MAX_SIZE", 2000):
        _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_goes_on_also_if_its_place_cannot_be_kept(mock_bot: AsyncMock) -> None:
    workflow = _three_steps()

    async def scenario(world: World) -> None:
        world.hold.open = True
        with patch.object(
            world.engine.store, "keep_place", AsyncMock(side_effect=OSError("Redis"))
        ), patch.object(
            world.engine.store, "drop_place", AsyncMock(side_effect=OSError("Redis"))
        ):
            await world.engine.save(workflow)
            await world.until(lambda: len(world.record.texts) == 2)
            await world.until(lambda: world.engine._sources[workflow.id].done())

        (run,) = await world.runs(workflow)
        assert run["status"] == "finished"

    _run(scenario)


# -- a run by hand -----------------------------------------------------------


def _asks() -> Workflow:
    return workflow_of(
        _step("one", "fake.record", "{{ trigger.value }}"),
        Step(id="ask", block="core.ask-text", inputs={"question": "What is new?"}),
        _step("two", "fake.record", "{{ ask.answer }}"),
        trigger="fake.asking",
    )


@patch_user_interaction("spanreed.workflows.blocks")
@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_by_hand_goes_on_and_asks_its_question_again(
    engine_bot: AsyncMock, bot: AsyncMock
) -> None:
    workflow = _asks()
    asked = asyncio.Event()

    async def scenario(world: World) -> None:
        async def no_answer(prompt: str) -> str:
            asked.set()
            await asyncio.Event().wait()
            return ""

        # The user starts it, and Spanreed stops before the answer.
        engine_bot.request_user_choice = AsyncMock(return_value=1)
        engine_bot.request_user_input = AsyncMock(return_value="the news")
        bot.request_user_input = AsyncMock(side_effect=no_answer)
        await world.engine.save(workflow)
        task = asyncio.create_task(
            world.engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        )
        await asked.wait()
        ((run_id, place),) = (await world.places(workflow)).items()
        assert place["manual"] is True
        assert [entry["id"] for entry in place["steps"]] == ["one"]

        bot.request_user_input = AsyncMock(return_value="much")
        await world.restart(tasks=[task])
        await world.until(lambda: len(world.record.texts) == 2)
        await world.until(lambda: not world.redis.hashes.get(PLACES))

        bot.request_user_input.assert_awaited_once_with("What is new?")
        assert world.record.texts == ["the news", "much"]
        (run,) = await world.runs(workflow)
        assert (run["id"], run["manual"], run["picked_up"]) == (run_id, True, 1)
        assert run["status"] == "finished"
        # The trigger hears how the run went, as after any run by hand.
        assert world.asking.done == [("the news", True)]
        # It spoke for itself: no summary.
        engine_bot.send_message.assert_not_awaited()

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_by_hand_that_did_not_speak_is_summed_up_when_it_went_on(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps(events=[])

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        task = asyncio.create_task(
            world.engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        )
        await world.until(lambda: world.hold.started)

        await world.restart(tasks=[task])
        await world.until(lambda: len(world.record.texts) == 2)
        await world.until(lambda: mock_bot.send_message.await_count)

        assert world.record.texts == ["by hand one", "by hand one, two, three (16)"]
        assert "finished" in mock_bot.send_message.await_args.args[0]

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_by_hand_that_the_user_stopped_does_not_come_back(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps(events=[])

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        task = asyncio.create_task(
            world.engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        )
        await world.until(lambda: world.hold.started)
        assert await world.places(workflow)

        # The user starts another command: this one is cancelled.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await world.places(workflow) == {}

        await world.restart()
        for _ in range(50):
            await asyncio.sleep(0)
        assert world.hold.started == ["by hand one, two"]

    _run(scenario)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_by_hand_is_forgotten_if_its_workflow_changed(
    mock_bot: AsyncMock,
) -> None:
    workflow = _three_steps(events=[])

    async def scenario(world: World) -> None:
        await world.engine.save(workflow)
        task = asyncio.create_task(
            world.engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        )
        await world.until(lambda: world.hold.started)
        workflow.steps[2].inputs["text"] = "another third step"
        await world.engine.store.save(workflow)

        await world.restart(kill=True, tasks=[task])
        for _ in range(50):
            await asyncio.sleep(0)

        assert await world.places(workflow) == {}
        assert world.record.texts == ["by hand one"]

    _run(scenario)


# -- what keeps no place -----------------------------------------------------


@patch_user_interaction("spanreed.workflows.engine")
def test_a_test_and_a_run_inside_a_step_keep_no_place(mock_bot: AsyncMock) -> None:
    part = workflow_of(
        _step("held", "fake.hold", "part"),
        trigger="core.called",
        id="wf_part",
        name="Part",
    )
    caller = workflow_of(
        Step(id="call", block="core.call-workflow", inputs={"workflow": "Part"}),
        events=["monday"],
    )

    async def scenario(world: World) -> None:
        await world.engine.save(part)
        await world.engine.save(caller)
        await world.until(lambda: world.hold.started)

        # The run that holds the step keeps a place. The run inside does not.
        assert len(await world.places(caller)) == 1
        assert await world.places(part) == {}

        tested = asyncio.create_task(
            world.engine.run_test(part, mock_user_find_by_id(4), "now")
        )
        for _ in range(50):
            await asyncio.sleep(0)
        assert await world.places(part) == {}
        tested.cancel()
        await asyncio.gather(tested, return_exceptions=True)

    _run(scenario)


# -- which event is the same -------------------------------------------------


def test_an_event_is_the_same_if_it_is_equal() -> None:
    trigger = Events()

    assert trigger.same_event({"value": "monday"}, Event("monday"))
    assert not trigger.same_event({"value": "monday"}, Event("tuesday"))


def test_the_first_event_of_a_schedule_stands_for_the_one_that_was_cut_short() -> None:
    kept = {"date": "2026-11-24", "time": "12:00"}
    later = Tick(date="2026-11-24", time="12:07")

    assert Schedule().same_event(kept, later)
    assert RandomSchedule().same_event(kept, later)


def test_what_a_poll_finds_is_the_same_if_the_thing_is_as_it_was() -> None:
    trigger = Polled()

    assert trigger.same_event({"value": "a@1"}, Event("a@1"))
    assert not trigger.same_event({"value": "a@1"}, Event("b@1"))
    # The thing changed since: the run is to start over with it as it is.
    assert not trigger.same_event({"value": "a@1"}, Event("a@2"))
    # An event that was kept without what names it is no event at all.
    assert not trigger.same_event({}, Event("a@1"))
