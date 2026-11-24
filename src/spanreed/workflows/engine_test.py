import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.interaction import SurfaceBusyError, commands
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.builtin import ContinueIf
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.recipes import Recipe
from spanreed.workflows.testing import (
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
        "Test workflow: 7 runs. 7 finished, 0 stopped, 0 failed.", parse_html=False
    )


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
