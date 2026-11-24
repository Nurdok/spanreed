"""Tests of how a run talks to the user: the turn, and what ends a question."""

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.interaction import UserInteractionPriority, commands
from spanreed.interaction import engine as turns
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.asking import AskText, Stop
from spanreed.workflows.blocks import Action, NoInput, StepContext
from spanreed.workflows.builtin import ForEach, If
from spanreed.workflows.engine import TOOK_OVER, WorkflowEngine
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.testing import (
    Record,
    drain,
    fake_storage,
    registry_of,
    workflow_of,
)

USER_ID = 4


def setup_function() -> None:
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()


@dataclass
class TurnOutput:
    """Whether the run had the user's turn to talk during a step."""

    has_turn: bool


class HasTurn(Action[NoInput, TurnOutput]):
    """An action that looks whether its run has the turn to talk."""

    id = "fake.has-turn"
    title = "Fake has turn"

    async def run(self, ctx: StepContext, inputs: NoInput) -> TurnOutput:
        """Return whether the task of the run has the turn."""
        return TurnOutput(has_turn=turns.holds_turn(ctx.user.id))


def _ask(step_id: str, question: str, **inputs: Any) -> Step:
    return Step(
        id=step_id, block="core.ask-text", inputs={"question": question, **inputs}
    )


def _has_turn(step_id: str) -> Step:
    return Step(id=step_id, block="fake.has-turn", inputs={})


def _engine(*blocks: Any) -> WorkflowEngine:
    return WorkflowEngine(registry_of(AskText(), HasTurn(), Record(), *blocks))


async def _runs(engine: WorkflowEngine, workflow: Workflow) -> list[dict[str, Any]]:
    return list(reversed(await engine.store.recent_runs(workflow.id, 100)))


def _sleeps() -> Any:
    return patch("spanreed.workflows.engine._sleep", new=AsyncMock())


class Waiting:
    """A question that the user has not answered yet."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self._answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def ask(self, prompt: str) -> str:
        self.asked.set()
        return await self._answer

    def answer(self, text: str) -> None:
        self._answer.set_result(text)


async def _soon(event: asyncio.Event) -> None:
    async with asyncio.timeout(3):
        await event.wait()


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_run_takes_the_turn_at_its_first_question_and_keeps_it(
    bot: AsyncMock, _: AsyncMock
) -> None:
    bot.request_user_input.return_value = " Dune "
    workflow = workflow_of(
        _has_turn("before"),
        _ask("book", "Which book?"),
        _has_turn("after"),
        events=["a"],
    )

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine()
        await engine.save(workflow)
        await drain(engine, workflow)
        # The turn is given up when the run is over.
        assert not turns.has_active_user_interaction(USER_ID)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    before, book, after = run["steps"]
    assert before["outputs"] == {"has_turn": False}
    assert book["outputs"] == {"answer": "Dune", "answered": True}
    assert after["outputs"] == {"has_turn": True}
    # What was asked and answered is kept with the step that asked.
    assert book["conversation"] == [{"asked": "Which book?", "answer": " Dune "}]
    assert "conversation" not in before


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_run_that_loses_the_turn_ends_without_failing(
    bot: AsyncMock, engine_bot: AsyncMock
) -> None:
    workflow = workflow_of(
        _ask("book", "Which book?", priority="low"),
        Step(id="note", block="fake.record", inputs={"text": "{{ book.answer }}"}),
        events=["a", "b"],
    )
    record = Record()

    async def scenario() -> list[dict[str, Any]]:
        first = Waiting()
        bot.request_user_input.side_effect = first.ask
        engine = WorkflowEngine(registry_of(AskText(), record))
        await engine.save(workflow)
        await _soon(first.asked)

        # Another conversation starts. A question of low priority gives way.
        second = Waiting()
        bot.request_user_input.side_effect = second.ask
        async with turns.user_interaction(USER_ID):
            pass

        # The workflow goes on with its next event.
        await _soon(second.asked)
        second.answer("Emma")
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        lost, answered = asyncio.run(scenario())

    assert lost["status"] == "stopped"
    (step,) = lost["steps"]
    assert (step["status"], step["reason"]) == ("stopped", TOOK_OVER)
    assert answered["status"] == "finished"
    assert record.texts == ["Emma"]
    # Not a failure: it is not tried again, and nobody is told.
    sleep.assert_not_awaited()
    engine_bot.notify.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_question_of_normal_priority_keeps_the_turn_from_another_one(
    bot: AsyncMock, _: AsyncMock
) -> None:
    workflow = workflow_of(_ask("book", "Which book?"), events=["a"])

    async def scenario() -> list[dict[str, Any]]:
        question = Waiting()
        bot.request_user_input.side_effect = question.ask
        engine = _engine()
        await engine.save(workflow)
        await _soon(question.asked)

        entered = asyncio.Event()

        async def other() -> None:
            async with turns.user_interaction(USER_ID):
                entered.set()

        task = asyncio.create_task(other())
        await asyncio.sleep(0.01)
        assert not entered.is_set()
        question.answer("Dune")
        await drain(engine, workflow)
        await task
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert run["status"] == "finished"


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_run_by_hand_asks_in_the_conversation_of_its_command(
    bot: AsyncMock, _: AsyncMock
) -> None:
    bot.request_user_input.return_value = "Dune"
    workflow = workflow_of(_ask("book", "Which book?"), _has_turn("after"))

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine()
        await engine.save(workflow)
        user = mock_user_find_by_id(USER_ID)
        # As the flow of a command does: take the turn, then call it.
        async with asyncio.timeout(3):
            async with turns.user_interaction(
                USER_ID, priority=UserInteractionPriority.HIGH, scope="web"
            ):
                await engine.run_by_hand(workflow.id, user)
                # The turn is the command's: the run did not give it up.
                assert turns.holds_turn(USER_ID)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert run["status"] == "finished"
    assert run["steps"][1]["outputs"] == {"has_turn": True}


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_run_that_waits_for_the_turn_lets_a_run_by_hand_go_first(
    bot: AsyncMock, _: AsyncMock
) -> None:
    """Else the two wait for each other.

    The run in the background has the workflow, and waits for the turn. The
    run by hand has the turn, and waits for the workflow.
    """
    bot.request_user_input.return_value = "Dune"
    workflow = workflow_of(
        Step(id="first", block="fake.record", inputs={"text": "{{ trigger.value }}"}),
        _ask("book", "Which book?"),
        events=["background"],
    )
    record = Record()

    async def scenario() -> list[dict[str, Any]]:
        engine = WorkflowEngine(registry_of(AskText(), record))
        user = mock_user_find_by_id(USER_ID)
        async with asyncio.timeout(3):
            async with turns.user_interaction(
                USER_ID, priority=UserInteractionPriority.HIGH
            ):
                await engine.save(workflow)
                # The run in the background gets to its question, and
                # waits there for the turn that this command has.
                while record.texts != ["background"]:
                    await asyncio.sleep(0)
                await asyncio.sleep(0.01)
                await engine.run_by_hand(workflow.id, user)
            await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        runs = asyncio.run(scenario())

    assert record.texts == ["background", "by hand"]
    # Kept in the order they ended: the run by hand went in between.
    assert [(run["manual"], run["status"]) for run in runs] == [
        (True, "finished"),
        (False, "finished"),
    ]


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_question_gives_up_after_its_minutes(
    bot: AsyncMock, engine_bot: AsyncMock
) -> None:
    workflow = workflow_of(
        _ask("book", "Which book?", give_up_after=1),
        Step(id="note", block="fake.record", inputs={"text": "{{ book.answer }}"}),
        events=["a"],
    )
    record = Record()

    async def scenario() -> list[dict[str, Any]]:
        bot.request_user_input.side_effect = Waiting().ask
        engine = WorkflowEngine(registry_of(AskText(), record))
        engine.GIVE_UP_UNIT_S = 0.01
        await engine.save(workflow)
        async with asyncio.timeout(3):
            await drain(engine, workflow)
        assert not turns.has_active_user_interaction(USER_ID)
        return await _runs(engine, workflow)

    with fake_storage(), _sleeps() as sleep:
        (run,) = asyncio.run(scenario())

    assert run["status"] == "stopped"
    assert run["steps"][0]["reason"] == "There was no answer for 1 minute."
    assert record.texts == []
    sleep.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_timeout_of_the_block_itself_is_a_failure(
    bot: AsyncMock, _: AsyncMock
) -> None:
    bot.request_user_input.side_effect = TimeoutError("Obsidian did not answer")
    workflow = workflow_of(_ask("book", "Which book?", give_up_after=5))

    async def scenario() -> list[dict[str, Any]]:
        engine = _engine()
        await engine.save(workflow)
        await engine.run_by_hand(workflow.id, mock_user_find_by_id(USER_ID))
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert run["status"] == "failed"
    assert run["steps"][0]["error"] == "Obsidian did not answer"


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_turning_a_workflow_off_ends_a_run_that_waits_for_an_answer(
    bot: AsyncMock, _: AsyncMock
) -> None:
    workflow = workflow_of(_ask("book", "Which book?"), events=["a"])

    async def scenario() -> list[dict[str, Any]]:
        question = Waiting()
        bot.request_user_input.side_effect = question.ask
        engine = _engine()
        await engine.save(workflow)
        await _soon(question.asked)
        assert turns.has_active_user_interaction(USER_ID)

        workflow.enabled = False
        await engine.save(workflow)
        await asyncio.sleep(0.01)
        # The turn is free for the next conversation.
        assert not turns.has_active_user_interaction(USER_ID)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())

    assert run["status"] == "cancelled"


def _run_with_a_stop_inside(holder: Step) -> tuple[dict[str, Any], list[str]]:
    """Run a workflow of ``holder`` and a step after it. Return the trace and
    what the steps recorded."""
    after = Step(id="after", block="fake.record", inputs={"text": "after"})
    workflow = workflow_of(holder, after, events=["a"])
    record = Record()

    async def scenario() -> list[dict[str, Any]]:
        engine = WorkflowEngine(registry_of(record, Stop(), ForEach(), If()))
        await engine.save(workflow)
        await drain(engine, workflow)
        return await _runs(engine, workflow)

    with fake_storage():
        (run,) = asyncio.run(scenario())
    return run, record.texts


_SAID = Step(id="said", block="fake.record", inputs={"text": "{{ trigger.value }}"})
_STOP = Step(id="stop", block="core.stop", inputs={"reason": "Enough."})


@patch_user_interaction("spanreed.workflows.engine")
def test_ending_the_run_inside_a_loop_ends_the_loop_too(_: AsyncMock) -> None:
    run, texts = _run_with_a_stop_inside(
        Step(
            id="each",
            block="core.for-each",
            inputs={"items": [1, 2, 3]},
            steps=[_SAID, _STOP],
        )
    )

    # One round of the loop, and nothing after it.
    assert texts == ["a"]
    assert run["status"] == "stopped"
    (step,) = run["steps"]
    assert (step["status"], step["reason"]) == ("stopped", "Enough.")
    assert step["rounds_run"] == 1
    assert [kept["status"] for kept in step["rounds"]] == ["stopped"]


@patch_user_interaction("spanreed.workflows.engine")
def test_ending_the_run_inside_a_branch_inside_a_loop_ends_all_of_it(
    _: AsyncMock,
) -> None:
    run, texts = _run_with_a_stop_inside(
        Step(
            id="each",
            block="core.for-each",
            inputs={"items": [1, 2, 3]},
            steps=[
                Step(
                    id="check",
                    block="core.if",
                    inputs={"condition": True},
                    steps=[_SAID, _STOP],
                )
            ],
        )
    )

    assert texts == ["a"]
    assert run["status"] == "stopped"
    assert run["steps"][0]["reason"] == "Enough."


@patch_user_interaction("spanreed.workflows.engine")
@patch_user_interaction("spanreed.workflows.blocks")
def test_a_workflow_that_asks_is_not_tested(bot: AsyncMock, _: AsyncMock) -> None:
    asks_inside = workflow_of(
        Step(
            id="check",
            block="core.if",
            inputs={"condition": True},
            otherwise=[_ask("book", "Which book?")],
        )
    )
    silent = workflow_of(_has_turn("one"), id="wf_2")

    async def scenario() -> None:
        engine = _engine(If())
        user = mock_user_find_by_id(USER_ID)
        assert engine.asks(asks_inside)
        assert not engine.asks(silent)
        assert (await engine.test_events(asks_inside))["asks"] is True
        with pytest.raises(ValueError, match="cannot be tested here"):
            await engine.run_test(asks_inside, user, "now")
        assert await engine.run_test(silent, user, "now") is not None

    with fake_storage():
        asyncio.run(scenario())

    bot.request_user_input.assert_not_awaited()
