import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.interaction import SurfaceBusyError, commands
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.blocks import BlockRegistry, NoQuestion
from spanreed.workflows.builtin import ContinueIf, ForEach, If
from spanreed.workflows.engine import WorkflowEngine, outcome
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.recipes import Recipe
from spanreed.workflows.testing import (
    Asking,
    Polled,
    Pushed,
    Record,
    drain,
    fake_storage,
    registry_of,
    workflow_of,
)


def setup_function() -> None:
    commands.reset_registry_for_tests()


def _record(step_id: str, text: str) -> Step:
    return Step(id=step_id, block="fake.record", inputs={"text": text})


def _engine(*blocks: Any, registered: bool = True) -> WorkflowEngine:
    return WorkflowEngine(registry_of(*blocks, registered=registered))


async def _runs(engine: WorkflowEngine, workflow: Workflow) -> list[dict[str, Any]]:
    """Return the traces of a workflow's runs, oldest first."""
    return list(reversed(await engine.store.recent_runs(workflow.id, 100)))


def _sleeps() -> Any:
    """Patch the engine's sleeps away, and collect how long they were."""
    return patch("spanreed.workflows.engine._sleep", new=AsyncMock())


# -- runs ------------------------------------------------------------------


@patch_user_interaction("spanreed.workflows.engine")
def test_each_event_is_a_run_and_steps_get_earlier_outputs(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(
        _record("one", "got {{ trigger.value }}"),
        _record("two", "{{ one.text }} ({{ one.length }})"),
        events=["a", "b"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        first, second = asyncio.run(scenario())

    assert record.texts == ["got a", "got a (5)", "got b", "got b (5)"]
    assert first["status"] == "finished"
    assert first["manual"] is False
    assert first["trigger"] == {"block": "fake.events", "outputs": {"value": "a"}}
    one, two = first["steps"]
    assert one["inputs"] == {"text": "got a"}
    assert one["outputs"] == {"text": "got a", "length": 5}
    assert (two["id"], two["title"], two["status"]) == ("two", "Fake record", "ok")
    assert second["trigger"]["outputs"] == {"value": "b"}


@patch_user_interaction("spanreed.workflows.engine")
def test_a_block_can_end_the_run_without_failing_it(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(
        Step(
            id="check",
            block="core.continue-if",
            inputs={"condition": "{{ trigger.value == 'go' }}"},
        ),
        _record("after", "went"),
        events=["stop", "go"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, ContinueIf())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        stopped, went = asyncio.run(scenario())

    assert stopped["status"] == "stopped"
    assert [step["status"] for step in stopped["steps"]] == ["stopped"]
    assert went["status"] == "finished"
    assert record.texts == ["went"]
    # A run that was stopped is not a failure: it is not tried again.
    sleep.assert_not_awaited()
    mock_bot.notify.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
def test_a_failed_run_is_tried_again_from_its_first_step(mock_bot: AsyncMock) -> None:
    first, second = Record(), Record()
    second.id = "fake.second"  # type: ignore[misc]
    second.failures = [RuntimeError("Todoist is down"), RuntimeError("still down")]
    workflow = workflow_of(
        _record("one", "first"),
        Step(id="two", block="fake.second", inputs={"text": "second"}),
        events=["a"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(first, second)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        runs = asyncio.run(scenario())

    assert [run["status"] for run in runs] == ["failed", "failed", "finished"]
    assert [run["attempt"] for run in runs] == [1, 2, 3]
    assert runs[0]["steps"][1]["error"] == "Todoist is down"
    # The pause doubles, and the first step ran again each time.
    assert [call.args[0] for call in sleep.await_args_list] == [60, 120]
    assert first.texts == ["first"] * 3
    assert second.texts == ["second"]
    mock_bot.notify.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
def test_the_user_is_told_once_that_a_workflow_keeps_failing(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    attempts = WorkflowEngine.MAX_ATTEMPTS
    # The first event fails every time it is tried, and so does the second.
    # The third works.
    record.failures = [RuntimeError("down")] * (attempts + 1)
    workflow = workflow_of(_record("one", "hello"), events=["a", "b", "c"])

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        runs = asyncio.run(scenario())

    # A workflow that fails gets one try for each event after the first.
    assert [run["trigger"]["outputs"]["value"] for run in runs] == (
        ["a"] * attempts + ["b", "c"]
    )
    assert [run["status"] for run in runs] == ["failed"] * (attempts + 1) + ["finished"]
    mock_bot.notify.assert_awaited_once()
    message = mock_bot.notify.call_args.args[0]
    assert "“Test workflow” failed at step 1, Fake record: down" in message
    assert record.texts == ["hello"]
    # And no sooner than half an hour after its last failure: the pauses
    # between the tries of "a", then one before "b" and one before "c".
    waits = [call.args[0] for call in sleep.await_args_list]
    assert waits[: attempts - 1] == [60, 120, 240, 480]
    assert len(waits) == attempts + 1
    assert all(1700 < wait <= 1800 for wait in waits[attempts - 1 :])


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_works_again_is_tried_in_full_the_next_time_it_fails(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    attempts = WorkflowEngine.MAX_ATTEMPTS
    # "a" fails every time, "b" works, "c" fails twice and then works.
    record.failures = [
        *([RuntimeError("down")] * attempts),
        None,
        *([RuntimeError("down again")] * 2),
    ]
    workflow = workflow_of(
        _record("one", "{{ trigger.value }}"), events=["a", "b", "c"]
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps():
        runs = asyncio.run(scenario())

    assert [run["attempt"] for run in runs][-3:] == [1, 2, 3]
    assert record.texts == ["b", "c"]
    mock_bot.notify.assert_awaited_once()


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_waits_out_flood_control_and_only_it_runs_again(
    mock_bot: AsyncMock,
) -> None:
    first, second = Record(), Record()
    second.id = "fake.second"  # type: ignore[misc]
    second.failures = [SurfaceBusyError(retry_after=30)]
    workflow = workflow_of(
        _record("one", "first"),
        Step(id="two", block="fake.second", inputs={"text": "second"}),
        events=["a"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(first, second)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        (run,) = asyncio.run(scenario())

    assert run["status"] == "finished"
    assert first.texts == ["first"]
    assert second.texts == ["second"]
    # The ban, plus the extra that spreads out the steps it stopped.
    (waited,) = (call.args[0] for call in sleep.await_args_list)
    assert 30 + 60 <= waited <= 30 + 600


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_fails_if_its_plugin_is_not_set_up(mock_bot: AsyncMock) -> None:
    workflow = workflow_of(_record("one", "hello"), events=["a"])

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(Record(), registered=False)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps():
        runs = asyncio.run(scenario())

    assert runs[0]["status"] == "failed"
    assert runs[0]["steps"][0]["error"].startswith("Fake is not set up.")


@patch_user_interaction("spanreed.workflows.engine")
def test_a_value_that_does_not_fit_the_block_fails_the_step(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    # The trigger's whole event is not a text.
    workflow = workflow_of(_record("one", "{{ trigger }}"), events=["a"])

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record)
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps():
        runs = asyncio.run(scenario())

    assert runs[0]["steps"][0]["error"] == "Text: Expected text."
    assert record.texts == []


@patch_user_interaction("spanreed.workflows.engine")
def test_a_trigger_that_fails_is_started_again_after_a_pause(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), events=["a"])

    async def scenario() -> None:
        engine = _engine(record)
        consume = engine._consume
        calls = 0

        async def consume_failing_once(workflow: Workflow) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("the service is down")
            await consume(workflow)

        engine._consume = consume_failing_once  # type: ignore[method-assign]
        await engine.save(workflow)
        await drain(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        asyncio.run(scenario())

    assert record.texts == ["a"]
    assert [call.args[0] for call in sleep.await_args_list] == [60]


# -- steps inside a loop -----------------------------------------------------


def _for_each(*steps: Step) -> Step:
    """Return a loop over the letters of the event: ``a,b,c``."""
    return Step(
        id="each",
        block="core.for-each",
        inputs={"items": "{{ trigger.value.split(',') if trigger.value else [] }}"},
        steps=list(steps),
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_the_steps_inside_a_loop_run_once_for_each_item(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(
        _for_each(
            _record("inner", "{{ each.item }} {{ each.index }}/{{ each.count }}")
        ),
        _record("after", "{{ each.count }} items"),
        events=["a,b,c"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, ForEach())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert record.texts == ["a 1/3", "b 2/3", "c 3/3", "3 items"]
    assert run["status"] == "finished"
    loop = run["steps"][0]
    assert (loop["round_name"], loop["round_count"], loop["rounds_run"]) == (
        "Item",
        3,
        3,
    )
    assert [r["number"] for r in loop["rounds"]] == [1, 2, 3]
    assert loop["rounds"][1]["steps"][0]["inputs"] == {"text": "b 2/3"}
    assert loop["outputs"] == {"item": None, "index": 0, "count": 3}


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_inside_a_loop_that_ends_the_run_ends_only_its_round(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    skip_b = Step(
        id="not_b",
        block="core.continue-if",
        inputs={"condition": "{{ each.item != 'b' }}"},
    )
    workflow = workflow_of(
        _for_each(skip_b, _record("inner", "{{ each.item }}")),
        _record("after", "done"),
        events=["a,b,c"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, ForEach(), ContinueIf())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert record.texts == ["a", "c", "done"]
    assert run["status"] == "finished"
    assert [r["status"] for r in run["steps"][0]["rounds"]] == [
        "finished",
        "stopped",
        "finished",
    ]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_inside_a_loop_that_fails_fails_the_run_and_it_starts_over(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    # "a" works, then "b" fails once.
    record.failures = [None, RuntimeError("Obsidian did not answer")]
    workflow = workflow_of(
        _for_each(_record("inner", "{{ each.item }}")),
        _record("after", "done"),
        events=["a,b,c"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, ForEach())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps():
        failed, worked = asyncio.run(scenario())

    # The second try wrote "a" again: a step must be safe to repeat.
    assert record.texts == ["a", "a", "b", "c", "done"]
    assert failed["status"] == "failed"
    assert failed["steps"][0]["error"] == (
        "Item 2 of 3, Fake record: Obsidian did not answer"
    )
    assert outcome(failed) == (
        "failed at step 1, For each: Item 2 of 3, Fake record: "
        "Obsidian did not answer"
    )
    assert [r["status"] for r in failed["steps"][0]["rounds"]] == ["finished", "failed"]
    # The third item was not got to.
    assert failed["steps"][0]["rounds_run"] == 2
    assert worked["status"] == "finished"


@patch_user_interaction("spanreed.workflows.engine")
def test_a_loop_over_nothing_runs_nothing_inside(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(
        _for_each(_record("inner", "{{ each.item }}")),
        _record("after", "{{ each.count }} items"),
        events=[""],
    )

    async def scenario() -> None:
        engine = _engine(record, ForEach())
        await engine.save(workflow)
        await drain(engine, workflow)

    with fake_storage():
        asyncio.run(scenario())

    assert record.texts == ["0 items"]


@patch_user_interaction("spanreed.workflows.engine")
def test_the_trace_of_a_long_loop_keeps_its_first_rounds(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(
        _for_each(_record("inner", "{{ each.item }}")), events=["a,b,c,d"]
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, ForEach())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), patch.object(WorkflowEngine, "ROUNDS_KEPT", 2):
        (run,) = asyncio.run(scenario())

    assert record.texts == ["a", "b", "c", "d"]
    loop = run["steps"][0]
    assert (loop["round_count"], loop["rounds_run"]) == (4, 4)
    assert [r["number"] for r in loop["rounds"]] == [1, 2]


# -- steps inside a branch ---------------------------------------------------


def _if(condition: str, then: list[Step], otherwise: list[Step]) -> Step:
    return Step(
        id="check",
        block="core.if",
        inputs={"condition": condition},
        steps=then,
        otherwise=otherwise,
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_a_branch_runs_one_of_its_sides_and_the_run_goes_on(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(
        _if(
            "{{ trigger.value == 'a' }}",
            [
                _record("yes", "yes {{ trigger.value }}"),
                _record("more", "len {{ yes.length }}"),
            ],
            [_record("no", "no {{ trigger.value }} {{ check.yes }}")],
        ),
        _record("after", "after {{ check.yes }}"),
        events=["a", "b"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, If())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        first, second = asyncio.run(scenario())

    assert record.texts == [
        "yes a",
        "len 5",
        "after True",
        "no b False",
        "after False",
    ]
    assert (first["status"], second["status"]) == ("finished", "finished")
    took = first["steps"][0]
    assert (took["took"], took["took_title"]) == ("steps", "If yes")
    assert [inner["id"] for inner in took["inside"]] == ["yes", "more"]
    assert took["outputs"] == {"yes": True}
    other = second["steps"][0]
    assert (other["took"], other["took_title"]) == ("otherwise", "Otherwise")
    assert [inner["id"] for inner in other["inside"]] == ["no"]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_side_with_no_steps_does_nothing(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(
        _if("{{ trigger.value == 'a' }}", [_record("yes", "yes")], []),
        _record("after", "after"),
        events=["b"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, If())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert record.texts == ["after"]
    assert run["steps"][0]["inside"] == []


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_inside_a_branch_that_fails_fails_the_run(mock_bot: AsyncMock) -> None:
    record = Record()
    record.failures = [RuntimeError("Obsidian did not answer")]
    workflow = workflow_of(
        _if("{{ true }}", [_record("yes", "yes")], []),
        _record("after", "after"),
        events=["a"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, If())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps():
        failed, worked = asyncio.run(scenario())

    assert outcome(failed) == (
        "failed at step 1, If: Fake record: Obsidian did not answer"
    )
    assert failed["steps"][0]["inside"][0]["status"] == "failed"
    assert worked["status"] == "finished"
    assert record.texts == ["yes", "after"]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_inside_a_branch_that_ends_the_run_ends_it(mock_bot: AsyncMock) -> None:
    record = Record()
    stop = Step(
        id="stop", block="core.continue-if", inputs={"condition": "{{ false }}"}
    )
    workflow = workflow_of(
        _if("{{ true }}", [stop, _record("yes", "yes")], []),
        _record("after", "after"),
        events=["a"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, If(), ContinueIf())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert record.texts == []
    assert run["status"] == "stopped"
    assert outcome(run) == "stopped at step 1, If."
    assert run["steps"][0]["reason"] == "The condition is false."


@patch_user_interaction("spanreed.workflows.engine")
def test_a_branch_inside_a_loop_that_ends_the_run_ends_only_its_round(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    stop = Step(
        id="stop", block="core.continue-if", inputs={"condition": "{{ false }}"}
    )
    check = Step(
        id="check",
        block="core.if",
        inputs={"condition": "{{ each.item == 'b' }}"},
        steps=[stop],
        otherwise=[_record("kept", "{{ each.item }}")],
    )
    workflow = workflow_of(
        _for_each(check, _record("inner", "{{ each.item }}!")),
        _record("after", "done"),
        events=["a,b,c"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record, If(), ForEach(), ContinueIf())
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert record.texts == ["a", "a!", "c", "c!", "done"]
    assert run["status"] == "finished"
    assert [r["status"] for r in run["steps"][0]["rounds"]] == [
        "finished",
        "stopped",
        "finished",
    ]


# -- triggers that a plugin feeds --------------------------------------------


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_pushed_key_is_a_run_and_leaves_the_queue_when_the_run_worked(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.pushed")

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        queue = engine.store.queue(workflow.id)

        await Pushed.emit(mock_user_find_by_id(4), ["monday", "nothing", "tuesday"])
        await _until(lambda: len(record.texts) == 2)
        await _until_empty(queue)

        # A key with no event is taken out without a run.
        assert record.texts == ["monday", "tuesday"]
        assert len(await _runs(engine, workflow)) == 2
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario())


async def _until_empty(queue: Any) -> None:
    async with asyncio.timeout(2):
        while await queue.keys():
            await asyncio.sleep(0)


@patch_user_interaction("spanreed.workflows.engine")
def test_a_key_whose_run_keeps_failing_stays_queued_and_is_tried_again(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    attempts = WorkflowEngine.MAX_ATTEMPTS
    # Obsidian is closed for all the tries of "monday", then it is back.
    record.failures = [RuntimeError("Obsidian did not answer")] * attempts
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.pushed")

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        queue = engine.store.queue(workflow.id)

        await Pushed.emit(mock_user_find_by_id(4), ["monday", "tuesday"])
        await _until_empty(queue)

        # "tuesday" ran while "monday" waited behind it, then "monday" worked.
        assert record.texts == ["tuesday", "monday"]
        runs = await _runs(engine, workflow)
        assert [run["status"] for run in runs] == ["failed"] * attempts + [
            "finished",
            "finished",
        ]
        mock_bot.notify.assert_awaited_once()
        await engine.stop()

    with fake_storage(), _sleeps():
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_keys_put_while_the_engine_was_down_run_when_it_starts(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.pushed")

    async def scenario(registry: BlockRegistry) -> None:
        store = WorkflowEngine(registry).store
        await store.save(workflow)
        await Pushed.emit(mock_user_find_by_id(4), ["monday"])

        engine = WorkflowEngine(registry)
        await engine.start([4])
        await _until_empty(engine.store.queue(workflow.id))
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario(registry_of(record)))

    assert record.texts == ["monday"]


# -- triggers that poll --------------------------------------------------------


def _no_wait_between_polls() -> Any:
    return patch("spanreed.workflows.blocks._sleep", new=AsyncMock())


@patch_user_interaction("spanreed.workflows.engine")
def test_what_a_poll_finds_is_a_run_once(mock_bot: AsyncMock) -> None:
    record = Record()
    polled = Polled(["a", "b"], ["a", "b", "c"], ["c"], waits=True)
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.polled")

    async def scenario() -> None:
        engine = _engine(record, polled)
        await engine.save(workflow)

        await _until(lambda: polled.waiting)

        assert record.texts == ["a", "b", "c"]
        assert len(await _runs(engine, workflow)) == 3
        await engine.stop()

    with fake_storage(), _no_wait_between_polls():
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_what_a_poll_found_runs_again_after_the_next_poll_if_its_run_failed(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    attempts = WorkflowEngine.MAX_ATTEMPTS
    # Obsidian is closed for all the tries of "a" in the first poll.
    record.failures = [RuntimeError("Obsidian did not answer")] * attempts
    polled = Polled(["a", "b"], ["a", "b"], waits=True)
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.polled")

    async def scenario() -> None:
        engine = _engine(record, polled)
        await engine.save(workflow)

        await _until(lambda: polled.waiting)

        # "b" ran in the first poll. The second poll gave "a" again.
        assert record.texts == ["b", "a"]
        # The cursor stayed where it was while a run of the poll failed.
        assert polled.cursors == [None, None]
        mock_bot.notify.assert_awaited_once()
        await engine.stop()

    with fake_storage(), _sleeps(), _no_wait_between_polls():
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_what_ran_is_not_run_again_after_a_restart(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.polled")

    async def scenario() -> None:
        first = Polled(["a", "b"], waits=True)
        engine = _engine(record, first)
        await engine.save(workflow)
        await _until(lambda: first.waiting)
        await engine.stop()

        # Spanreed starts again, and the service still has "a" and "b".
        second = Polled(["a", "b", "c"], waits=True)
        engine = _engine(record, second)
        await engine.start([4])
        await _until(lambda: second.waiting)
        await engine.stop()

        assert record.texts == ["a", "b", "c"]
        # The second engine went on from the cursor of the first.
        assert second.cursors == ["after poll 1"]

    with fake_storage(), _no_wait_between_polls():
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_install_can_change_the_workflow_before_it_is_stored(
    mock_bot: AsyncMock,
) -> None:
    recipe = Recipe(
        id="demo",
        name="Demo",
        description="",
        trigger={"block": "fake.pushed", "params": {}},
        steps=[{"id": "one", "block": "fake.record", "inputs": {"text": "a"}}],
    )

    def customize(workflow: Workflow) -> None:
        workflow.steps[0].inputs["text"] = "from the old settings"
        workflow.enabled = False

    async def scenario() -> None:
        engine = _engine(Record())
        workflow = await engine.install(mock_user_find_by_id(4), recipe, customize)
        stored = await engine.store.get(workflow.id)
        assert stored is not None
        assert stored.steps[0].inputs == {"text": "from the old settings"}
        assert not stored.enabled
        # The recipe itself is as it was.
        assert recipe.steps[0]["inputs"] == {"text": "a"}

    with fake_storage():
        asyncio.run(scenario())


# -- what runs ---------------------------------------------------------------


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_is_off_or_broken_does_not_run(mock_bot: AsyncMock) -> None:
    record = Record()
    off = workflow_of(_record("one", "off"), events=["a"], id="wf_off", enabled=False)
    broken = workflow_of(
        Step(id="one", block="fake.nothing"), events=["a"], id="wf_broken"
    )

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(off)
        await engine.save(broken)
        assert not engine._sources
        assert engine.problems(broken) == ["Step 1: there is no block “fake.nothing”."]

    with fake_storage():
        asyncio.run(scenario())

    assert record.texts == []


@patch_user_interaction("spanreed.workflows.engine")
def test_start_runs_the_stored_workflows_of_the_users(mock_bot: AsyncMock) -> None:
    record = Record()
    mine = workflow_of(_record("one", "mine"), events=["a"], id="wf_mine")
    other = workflow_of(_record("one", "other"), events=["a"], id="wf_other", user_id=7)

    async def scenario(registry: BlockRegistry) -> None:
        before = WorkflowEngine(registry)
        await before.store.save(mine)
        await before.store.save(other)

        # A new engine, as after a restart.
        engine = WorkflowEngine(registry)
        await engine.start([4])
        await drain(engine, mine)
        assert list(engine._sources) == ["wf_mine"]
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario(registry_of(record)))

    assert record.texts == ["mine"]


@patch_user_interaction("spanreed.workflows.engine")
def test_turning_a_workflow_off_stops_its_trigger(mock_bot: AsyncMock) -> None:
    workflow = workflow_of(_record("one", "hello"), events=["a"])

    async def scenario() -> None:
        engine = _engine(Record())
        await engine.save(workflow)
        task = engine._sources[workflow.id]

        workflow.enabled = False
        await engine.save(workflow)
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert not engine._sources

    with fake_storage():
        asyncio.run(scenario())


# -- commands ----------------------------------------------------------------


def _command_texts(user_id: int) -> list[str]:
    user = mock_user_find_by_id(user_id)
    user.plugins = ["workflows"]
    return [command.text for command in commands.commands_for_user(user)]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_can_be_started_by_hand_is_a_command_of_its_user(
    mock_bot: AsyncMock,
) -> None:
    workflow = workflow_of(_record("one", "hello"))

    async def scenario() -> None:
        engine = _engine(Record())
        await engine.save(workflow)
        assert _command_texts(4) == ["Test workflow"]
        assert _command_texts(7) == []
        (command,) = commands.commands_for_user(_user_with_workflows())
        assert command.id == "workflow.wf_1"
        assert command.plugin_canonical_name == "workflows"

        # A new name keeps the command's id.
        workflow.command_name = "Do it"
        await engine.save(workflow)
        assert _command_texts(4) == ["Do it"]
        assert commands.get_command("workflow.wf_1") is not None

        workflow.command_hidden = True
        await engine.save(workflow)
        assert _command_texts(4) == []

        workflow.command_hidden = False
        workflow.enabled = False
        await engine.save(workflow)
        assert _command_texts(4) == []

        workflow.enabled = True
        await engine.save(workflow)
        assert _command_texts(4) == ["Do it"]
        await engine.delete(workflow)
        assert _command_texts(4) == []
        assert await engine.store.get(workflow.id) is None

    with fake_storage():
        asyncio.run(scenario())


def _user_with_workflows() -> Any:
    user = mock_user_find_by_id(4)
    user.plugins = ["workflows"]
    return user


@patch_user_interaction("spanreed.workflows.engine")
def test_a_trigger_that_cannot_be_started_by_hand_gives_no_command(
    mock_bot: AsyncMock,
) -> None:
    workflow = workflow_of(_record("one", "hello"), trigger="fake.silent")

    async def scenario() -> None:
        engine = _engine(Record())
        await engine.save(workflow)
        assert not engine.can_run_by_hand(workflow)
        assert _command_texts(4) == []

        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))

    with fake_storage():
        asyncio.run(scenario())

    assert "cannot be started by hand" in mock_bot.send_message.call_args.args[0]


# -- by hand -----------------------------------------------------------------


@patch_user_interaction("spanreed.workflows.engine")
def test_the_command_runs_the_workflow_and_says_how_it_went(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}, {{ run.manual }}"))

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record)
        await engine.save(workflow)
        (command,) = commands.commands_for_user(_user_with_workflows())
        await command.callback(mock_user_find_by_id(4))
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert record.texts == ["by hand, True"]
    assert run["manual"] is True
    mock_bot.send_message.assert_awaited_once_with(
        "Test workflow: finished.", parse_html=False
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_a_run_by_hand_that_fails_says_why_and_is_not_tried_again(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    record.failures = [SurfaceBusyError(retry_after=30)]
    workflow = workflow_of(_record("one", "hello"))

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))

    with fake_storage(), _sleeps() as sleep:
        asyncio.run(scenario())

    # The user is there: no waiting for flood control, and no second try.
    sleep.assert_not_awaited()
    assert record.texts == []
    message = mock_bot.send_message.call_args.args[0]
    assert message.startswith("Test workflow: failed at step 1, Fake record:")


@patch_user_interaction("spanreed.workflows.engine")
def test_a_trigger_can_ask_before_a_run_by_hand(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.ranged")

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        mock_bot.request_user_choice.return_value = 1  # The last week
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))

    with fake_storage():
        asyncio.run(scenario())

    mock_bot.request_user_choice.assert_awaited_once_with(
        "Run “Test workflow” for…", ["Today", "The last week", "Cancel"]
    )
    assert record.texts == [f"day {day}" for day in range(7)]
    mock_bot.send_message.assert_awaited_once_with(
        "Test workflow: 7 runs. 7 finished, 0 ended early, 0 failed.", parse_html=False
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_a_trigger_can_ask_for_a_text_after_a_choice_that_needs_one(
    mock_bot: AsyncMock,
) -> None:
    record, asking = Record(), Asking()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.asking")
    mock_bot.request_user_choice.return_value = 1  # One
    mock_bot.request_user_input.return_value = " the red one \n"

    async def scenario() -> None:
        engine = _engine(record, asking)
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario())

    mock_bot.request_user_input.assert_awaited_once_with("Which one?")
    assert record.texts == ["the red one"]
    # The trigger is told how the run went.
    assert asking.done == [("the red one", True)]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_question_whose_choice_was_not_picked_is_not_asked(
    mock_bot: AsyncMock,
) -> None:
    record, asking = Record(), Asking()
    record.failures = [None, RuntimeError("boom")]
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.asking")
    mock_bot.request_user_choice.return_value = 0  # Everything

    async def scenario() -> None:
        engine = _engine(record, asking)
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario())

    mock_bot.request_user_input.assert_not_called()
    assert asking.done == [("first", True), ("second", False)]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_trigger_can_say_why_there_is_nothing_to_run_by_hand(
    mock_bot: AsyncMock,
) -> None:
    record, asking = Record(), Asking()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.asking")
    mock_bot.request_user_choice.return_value = 1  # One
    mock_bot.request_user_input.return_value = "nothing"

    async def scenario() -> None:
        engine = _engine(record, asking)
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        assert await _runs(engine, workflow) == []
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario())

    mock_bot.send_message.assert_awaited_once_with(
        "There is nothing of that name.", parse_html=False
    )


@patch_user_interaction("spanreed.workflows.engine")
def test_a_poll_does_not_run_again_for_what_a_run_by_hand_worked_for(
    mock_bot: AsyncMock,
) -> None:
    record = Record()

    class ByHand(Polled):
        Manual = NoQuestion

        async def manual_events(self, ctx: Any, params: Any, answer: Any) -> list[Any]:
            return await self.new_events(ctx, params)

    # The check by hand finds "a" and "b". Then the first poll does too.
    polled = ByHand(["a", "b"], ["a", "b", "c"], waits=True)
    # By hand, the run for "b" fails.
    record.failures = [None, RuntimeError("boom")]
    workflow = workflow_of(
        _record("one", "{{ trigger.value }}"), trigger="fake.polled", enabled=False
    )

    async def scenario() -> None:
        engine = _engine(record, polled)
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))
        assert record.texts == ["a"]

        workflow.enabled = True
        await engine.save(workflow)
        await _until(lambda: polled.waiting)
        await engine.stop()

    with fake_storage(), _no_wait_between_polls():
        asyncio.run(scenario())

    # The poll ran for "b", which failed by hand, and for "c". Not for "a".
    assert record.texts == ["a", "b", "c"]
    # The check by hand left the cursor where it was.
    assert polled.cursors == [None, None]


@patch_user_interaction("spanreed.workflows.engine")
def test_cancelling_the_question_runs_nothing(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), trigger="fake.ranged")

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        mock_bot.request_user_choice.return_value = 2  # Cancel
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(4))

    with fake_storage():
        asyncio.run(scenario())

    assert record.texts == []
    mock_bot.send_message.assert_not_awaited()


# -- trying a workflow out -----------------------------------------------------


@patch_user_interaction("spanreed.workflows.engine")
def test_the_event_of_the_last_run_is_kept_whole(mock_bot: AsyncMock) -> None:
    record = Record()
    long_text = "x" * 5000
    workflow = workflow_of(_record("one", "seen"), events=["first", long_text])

    async def scenario() -> tuple[Any, Any]:
        engine = _engine(record)
        await engine.save(workflow)
        await drain(engine, workflow)
        (run, *_) = await engine.store.recent_runs(workflow.id, 1)
        return await engine.test_events(workflow), run

    with fake_storage():
        events, run = asyncio.run(scenario())

    # The trace keeps the start of a long value. The event is all there,
    # so that a test does not write a shortened value.
    assert len(run["trigger"]["outputs"]["value"]) < 5000
    assert events["last"]["event"] == {"value": long_text}
    assert events["last"]["at"] == run["started_at"]
    # The fake trigger makes an event without asking.
    assert events["now"] is True


@patch_user_interaction("spanreed.workflows.engine")
def test_a_test_runs_the_workflow_it_is_given_for_the_last_event(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    stored = workflow_of(
        _record("one", "stored {{ trigger.value }}"), events=["monday"]
    )

    async def scenario() -> dict[str, Any] | None:
        engine = _engine(record)
        await engine.save(stored)
        await drain(engine, stored)
        # What the editor holds: not saved.
        draft = workflow_of(
            _record("new", "draft {{ trigger.value }}"), events=["monday"]
        )
        trace = await engine.run_test(draft, mock_user_find_by_id(4), "last")
        kept = await engine.store.get(stored.id)
        assert kept is not None and kept.steps[0].id == "one"
        # The test is in the history, marked. It did not become the last event.
        newest, oldest = await engine.store.recent_runs(stored.id, 10)
        assert (newest.get("test"), oldest.get("test")) == (True, None)
        assert (await engine.test_events(stored))["last"]["at"] == oldest["started_at"]
        await engine.stop()
        return trace

    with fake_storage():
        trace = asyncio.run(scenario())

    assert record.texts == ["stored monday", "draft monday"]
    assert trace is not None
    assert (trace["status"], trace["manual"], trace["test"]) == ("finished", True, True)
    assert trace["steps"][0]["inputs"] == {"text": "draft monday"}


@patch_user_interaction("spanreed.workflows.engine")
def test_a_test_that_fails_is_not_tried_again(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), events=["monday"])

    async def scenario() -> dict[str, Any] | None:
        engine = _engine(record)
        await engine.save(workflow)
        await drain(engine, workflow)
        record.failures = [RuntimeError("boom")]
        trace = await engine.run_test(workflow, mock_user_find_by_id(4), "last")
        await engine.stop()
        return trace

    with fake_storage(), _sleeps():
        trace = asyncio.run(scenario())

    assert trace is not None and trace["status"] == "failed"
    assert record.texts == ["monday"]
    mock_bot.notify.assert_not_called()


@patch_user_interaction("spanreed.workflows.engine")
def test_a_workflow_that_never_ran_is_tested_with_an_event_made_now(
    mock_bot: AsyncMock,
) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "{{ trigger.value }}"), enabled=False)
    asking = workflow_of(
        _record("one", "{{ trigger.value }}"), trigger="fake.ranged", enabled=False
    )
    asking.id = "wf_2"

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        await engine.save(asking)
        user = mock_user_find_by_id(4)
        assert await engine.test_events(workflow) == {"last": None, "now": True}
        assert await engine.run_test(workflow, user, "last") is None
        trace = await engine.run_test(workflow, user, "now")
        assert trace is not None and trace["status"] == "finished"
        # A trigger that asks first cannot make an event by itself.
        assert await engine.test_events(asking) == {"last": None, "now": False}
        assert await engine.run_test(asking, user, "now") is None

    with fake_storage():
        asyncio.run(scenario())

    assert record.texts == ["by hand"]


@patch_user_interaction("spanreed.workflows.engine")
def test_a_user_cannot_run_the_workflow_of_another_user(mock_bot: AsyncMock) -> None:
    record = Record()
    workflow = workflow_of(_record("one", "hello"))

    async def scenario() -> None:
        engine = _engine(record)
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(7))

    with fake_storage():
        asyncio.run(scenario())

    assert record.texts == []
    mock_bot.send_message.assert_awaited_once_with("This workflow no longer exists.")
