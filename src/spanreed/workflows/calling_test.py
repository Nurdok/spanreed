"""Tests of one workflow running another."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.interaction import commands
from spanreed.interaction import engine as turns
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.asking import AskText
from spanreed.workflows.blocks import BlockRegistry, NoParams, NoQuestion
from spanreed.workflows.builtin import SendMessage
from spanreed.workflows.calling import (
    _is_on,
    CallWorkflow,
    RunOne,
    RunOneInput,
    WhenCalled,
    calling_blocks,
)
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, TriggerSpec, Workflow
from spanreed.workflows.testing import (
    Record,
    drain,
    fake_plugin,
    fake_storage,
    registry_of,
    workflow_of,
)
from spanreed.workflows.values import from_data

USER_ID = 4


def setup_function() -> None:
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()


def _engine(record: Record) -> WorkflowEngine:
    return WorkflowEngine(
        registry_of(
            record, WhenCalled(), CallWorkflow(), RunOne(), AskText(), SendMessage()
        )
    )


def _part(name: str, *steps: Step, **fields: Any) -> Workflow:
    """Return a workflow that others run, with these steps."""
    return Workflow(
        id=f"wf_{name.lower().replace(' ', '_')}",
        user_id=USER_ID,
        name=name,
        trigger=TriggerSpec(block="core.called", params={}),
        steps=list(steps),
        **fields,
    )


def _say(step_id: str, text: str) -> Step:
    return Step(id=step_id, block="fake.record", inputs={"text": text})


def _call(name: str) -> Step:
    return Step(id="call", block="core.call-workflow", inputs={"workflow": name})


async def _runs(engine: WorkflowEngine, workflow: Workflow) -> list[dict[str, Any]]:
    return list(reversed(await engine.store.recent_runs(workflow.id, 100)))


def _sleeps() -> Any:
    return patch("spanreed.workflows.engine._sleep", new=AsyncMock())


def test_the_calling_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in calling_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in calling_blocks()] == [
        "core.called",
        "core.call-workflow",
        "core.run-one",
    ]
    assert WhenCalled.Manual is NoQuestion


def test_a_workflow_for_others_starts_no_run_by_itself() -> None:
    async def scenario() -> None:
        events = WhenCalled().events(AsyncMock(), NoParams())
        waiting = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.01)
        assert not waiting.done()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.engine")
def test_a_step_runs_another_workflow_and_goes_on_when_it_is_over(
    _: AsyncMock,
) -> None:
    record = Record()
    part = _part("Greeting", _say("hello", "hello from the part"))
    main = workflow_of(
        _say("before", "before"),
        _call("Greeting"),
        _say("after", "it {{ call.status }}: {{ call.finished }}"),
        events=["a"],
        name="Main",
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        engine = _engine(record)
        await engine.save(part)
        await engine.save(main)
        await drain(engine, main)
        (ran,) = await _runs(engine, main)
        (called,) = await _runs(engine, part)
        await engine.stop()
        return ran, called

    with fake_storage():
        ran, called = asyncio.run(scenario())

    assert record.texts == ["before", "hello from the part", "it finished: True"]
    assert ran["status"] == "finished"
    assert ran["steps"][1]["outputs"] == {
        "status": "finished",
        "finished": True,
        "outcome": "finished.",
    }
    # The other workflow's run is in its own history, and says who ran it.
    assert (called["status"], called["called_by"], called["manual"]) == (
        "finished",
        "Main",
        False,
    )
    assert "called_by" not in ran


@patch_user_interaction("spanreed.workflows.engine")
def test_a_part_that_fails_fails_the_step_that_ran_it(bot: AsyncMock) -> None:
    record = Record()
    record.failures = [RuntimeError("the vault is closed")] * 5
    part = _part("Greeting", _say("hello", "hello"))
    main = workflow_of(_call("Greeting"), _say("after", "after"), name="Main")

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine(record)
        await engine.save(part)
        await engine.save(main)
        await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
        (ran,) = await _runs(engine, main)
        called = await _runs(engine, part)
        await engine.stop()
        return [ran, *called]

    with fake_storage():
        ran, called = asyncio.run(scenario())

    # The part is not tried again by itself: the run that needed it failed.
    assert called["status"] == "failed"
    assert ran["status"] == "failed"
    assert ran["steps"][0]["error"] == (
        "“Greeting” failed at step 1, Fake record: the vault is closed"
    )
    assert record.texts == []


@patch_user_interaction("spanreed.workflows.engine")
def test_what_cannot_be_run_says_why(_: AsyncMock) -> None:
    record = Record()
    off = _part("Off", _say("x", "x"), enabled=False)
    asks_first = workflow_of(
        _say("x", "x"), trigger="fake.ranged", id="wf_ranged", name="Asks first"
    )
    loop_back = _part("Back", _call("Main"))
    main = workflow_of(name="Main")

    async def scenario() -> dict[str, str]:
        engine = _engine(record)
        for workflow in (off, asks_first, loop_back):
            await engine.save(workflow)
        errors = {}
        for name in ("Nothing", "Off", "Asks first", "Back", "Main"):
            main.steps = [_call(name)]
            await engine.save(main)
            await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
            (ran,) = await engine.store.recent_runs(main.id, 1)
            errors[name] = ran["steps"][0]["error"]
        await engine.stop()
        return errors

    with fake_storage():
        errors = asyncio.run(scenario())

    assert errors == {
        "Nothing": "There is not one workflow called “Nothing”.",
        "Off": "“Off” is off.",
        "Asks first": (
            "“Asks first” cannot be run by another workflow: its trigger asks "
            "before it starts, or cannot be started by hand."
        ),
        # Main runs Back, which would run Main.
        "Back": (
            "“Back” failed at step 1, Run a workflow: “Main” is running: it "
            "cannot run itself."
        ),
        "Main": "“Main” is running: it cannot run itself.",
    }
    assert record.texts == []


@patch_user_interaction("spanreed.workflows.engine")
def test_a_part_that_is_in_a_run_of_its_own_is_waited_for_and_then_given_up(
    _: AsyncMock,
) -> None:
    record = Record()
    part = _part("Greeting", _say("hello", "hello"))
    main = workflow_of(_call("Greeting"), name="Main")

    async def scenario() -> dict[str, Any]:
        engine = _engine(record)
        engine.CALL_WAIT_S = 0.02
        await engine.save(part)
        await engine.save(main)
        # As while another run of the part is going.
        lock = engine._locks.setdefault(part.id, asyncio.Lock())
        async with lock:
            await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
        (ran,) = await engine.store.recent_runs(main.id, 1)
        await engine.stop()
        return ran

    with fake_storage():
        ran = asyncio.run(scenario())

    assert ran["status"] == "failed"
    assert ran["steps"][0]["error"].startswith(
        "“Greeting” is in a run of its own, and still was after"
    )


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_part_asks_in_the_conversation_of_the_run_that_runs_it(
    bot: AsyncMock, engine_bot: AsyncMock
) -> None:
    bot.request_user_input.return_value = "Dune"
    record = Record()
    part = _part(
        "Ask",
        Step(id="book", block="core.ask-text", inputs={"question": "Which book?"}),
        _say("got", "{{ book.answer }}"),
    )
    main = workflow_of(_call("Ask"), _say("after", "after"), name="Main")

    async def scenario() -> list[str]:
        engine = _engine(record)
        await engine.save(part)
        await engine.save(main)
        async with turns.user_interaction(USER_ID, scope="web"):
            async with asyncio.timeout(3):
                await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
            assert turns.holds_turn(USER_ID)
        (ran,) = await engine.store.recent_runs(main.id, 1)
        # What the part said counts as said by the run: it is not summed up.
        assert ran["talked"] is True
        await engine.stop()
        return [call.args[0] for call in engine_bot.send_message.await_args_list]

    with fake_storage():
        said = asyncio.run(scenario())

    assert record.texts == ["Dune", "after"]
    assert said == []


# -- one of several ----------------------------------------------------------


def _one_of(workflows: dict[str, Any], **inputs: Any) -> Step:
    return Step(
        id="one", block="core.run-one", inputs={"workflows": workflows, **inputs}
    )


def _parts() -> list[Workflow]:
    return [
        _part("Journal", _say("j", "journal")),
        _part("Mood", _say("m", "mood")),
        _part("Books", _say("b", "books")),
        _part("Scans", _say("s", "scans"), enabled=False),
    ]


@patch_user_interaction("spanreed.workflows.engine")
def test_one_workflow_is_run_at_random_among_those_on_offer(_: AsyncMock) -> None:
    record = Record()
    main = workflow_of(
        _one_of(
            {
                "Journal": True,
                # A condition, from the event of the run.
                "Mood": "{{ trigger.value == 'by hand' }}",
                "Books": "no",
                # Off, so not on offer whatever its row says.
                "Scans": "yes",
                "Gone": True,
            }
        ),
        _say("after", "ran {{ one.ran }}: {{ one.status }}"),
        name="Main",
    )
    seen: list[list[str]] = []

    def last(options: list[str]) -> str:
        seen.append(list(options))
        return options[-1]

    async def scenario() -> dict[str, Any]:
        engine = _engine(record)
        for part in _parts():
            await engine.save(part)
        await engine.save(main)
        with patch("spanreed.workflows.calling._choice", new=last):
            await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
        (ran,) = await engine.store.recent_runs(main.id, 1)
        await engine.stop()
        return ran

    with fake_storage():
        ran = asyncio.run(scenario())

    assert seen == [["Journal", "Mood"]]
    assert record.texts == ["mood", "ran Mood: finished"]
    assert ran["steps"][0]["outputs"] == {
        "ran": "Mood",
        "status": "finished",
        "on_offer": ["Journal", "Mood"],
    }


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_the_user_can_be_the_one_to_pick(bot: AsyncMock, _: AsyncMock) -> None:
    bot.request_user_choice.return_value = 1
    record = Record()
    main = workflow_of(
        _one_of(
            {"Journal": True, "Books": True, "Mood": False},
            # By hand, the user picks. By itself, it is one at random.
            how="{{ 'ask me' if run.manual else 'at random' }}",
            question="What's your poison?",
        ),
        name="Main",
    )

    async def scenario() -> None:
        engine = _engine(record)
        for part in _parts():
            await engine.save(part)
        await engine.save(main)
        await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario())

    bot.request_user_choice.assert_awaited_once_with(
        "What's your poison?", ["Journal", "Books"], columns=1
    )
    assert record.texts == ["books"]


@patch_user_interaction("spanreed.workflows.engine")
def test_nothing_runs_when_nothing_is_on_offer(_: AsyncMock) -> None:
    record = Record()
    main = workflow_of(
        _one_of({"Scans": True, "Mood": 0}),
        _say("after", "ran [{{ one.ran }}]"),
        name="Main",
    )

    async def scenario() -> None:
        engine = _engine(record)
        for part in _parts():
            await engine.save(part)
        await engine.save(main)
        await engine.run_by_hand(main.id, mock_user_find_by_id(USER_ID))
        await engine.stop()

    with fake_storage():
        asyncio.run(scenario())

    assert record.texts == ["ran []"]


def test_a_row_says_yes_or_no_in_several_ways() -> None:
    given = from_data(
        RunOneInput,
        {
            "workflows": {
                "a": "yes",
                "b": "No",
                "c": True,
                "d": "",
                "e": "false",
                "f": 1,
            }
        },
    )
    assert [name for name, on in given.workflows.items() if _is_on(on)] == [
        "a",
        "c",
        "f",
    ]
