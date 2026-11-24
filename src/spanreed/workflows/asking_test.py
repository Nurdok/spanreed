"""Tests of the blocks that ask the user something."""

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.asking import (
    AskChoice,
    AskChoiceInput,
    AskEach,
    AskEachInput,
    AskList,
    AskListInput,
    AskText,
    AskTextInput,
    Command,
    Stop,
    StopInput,
    asking_blocks,
    option_text,
)
from spanreed.workflows.blocks import (
    Action,
    BlockRegistry,
    NoParams,
    NoQuestion,
    RunInfo,
    StepContext,
    StopRun,
    TriggerContext,
)
from spanreed.workflows.testing import fake_plugin
from spanreed.workflows.values import from_data


def _ctx() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="run_1", manual=True),
    )


def _choices(bot: AsyncMock) -> list[tuple[str, list[Any]]]:
    """Return each question that was asked with choices, and the choices."""
    return [
        (call.args[0], call.args[1]) for call in bot.request_user_choice.await_args_list
    ]


def test_the_asking_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in asking_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in asking_blocks()] == [
        "core.command",
        "core.ask-choice",
        "core.ask-each",
        "core.ask-text",
        "core.ask-list",
        "core.stop",
    ]
    asks = [b for b in asking_blocks() if isinstance(b, Action) and b.converses]
    assert [block.id for block in asks] == [
        "core.ask-choice",
        "core.ask-each",
        "core.ask-text",
        "core.ask-list",
    ]
    # Every block that asks says how its run waits for the turn.
    needed: dict[str, dict[str, Any]] = {
        "core.ask-choice": {"question": "?", "options": []},
        "core.ask-each": {"question": "?", "options": {}},
        "core.ask-text": {"question": "?"},
        "core.ask-list": {"question": "?", "add_question": "?"},
    }
    for block in asks:
        waits = from_data(
            block.Input, {**needed[block.id], "priority": "low", "give_up_after": 5}
        )
        assert (waits.priority, waits.give_up_after) == ("low", 5)
        plain = from_data(block.Input, needed[block.id])
        assert (plain.priority, plain.give_up_after) == ("normal", 0)


# -- text --------------------------------------------------------------------


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_text_returns_what_the_user_wrote(bot: AsyncMock) -> None:
    bot.request_user_input.return_value = "  Dune \n"

    output = asyncio.run(AskText().run(_ctx(), AskTextInput(question="Which book?")))

    assert (output.answer, output.answered) == ("Dune", True)
    bot.request_user_input.assert_awaited_once_with("Which book?")
    bot.request_user_choice.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_text_can_ask_first_whether_there_is_an_answer(bot: AsyncMock) -> None:
    inputs = AskTextInput(question="Enter free text:", ask_first="Any free text?")

    bot.request_user_choice.return_value = 1  # No
    declined = asyncio.run(AskText().run(_ctx(), inputs))
    assert (declined.answer, declined.answered) == ("", False)
    bot.request_user_input.assert_not_awaited()

    bot.request_user_choice.return_value = 0  # Yes
    bot.request_user_input.return_value = "A gift."
    given = asyncio.run(AskText().run(_ctx(), inputs))
    assert (given.answer, given.answered) == ("A gift.", True)
    assert _choices(bot)[0] == ("Any free text?", ["Yes", "No"])


# -- a list of texts ---------------------------------------------------------


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_list_asks_until_the_user_says_that_was_all(bot: AsyncMock) -> None:
    bot.request_user_choice.side_effect = [0, 0, 1]
    bot.request_user_input.side_effect = ["Dana", " Omer "]

    output = asyncio.run(
        AskList().run(
            _ctx(),
            AskListInput(
                add_question='Add "Recommended by"?',
                question="Who recommended it?",
                add_another_question='Add another "Recommended by"?',
            ),
        )
    )

    assert (output.answers, output.count) == (["Dana", "Omer"], 2)
    assert [question for question, _ in _choices(bot)] == [
        'Add "Recommended by"?',
        'Add another "Recommended by"?',
        'Add another "Recommended by"?',
    ]


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_list_can_come_back_empty(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 1

    output = asyncio.run(
        AskList().run(_ctx(), AskListInput(add_question="Add one?", question="Who?"))
    )

    assert (output.answers, output.count) == ([], 0)
    bot.request_user_input.assert_not_awaited()


# -- a choice ----------------------------------------------------------------


@dataclass
class Found:
    """Something a search found, with the text that stands for it."""

    label: str
    year: int


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_returns_the_text_and_its_place(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 1

    output = asyncio.run(
        AskChoice().run(
            _ctx(), AskChoiceInput(question="Which?", options=["Tea", "Coffee"])
        )
    )

    assert (output.chosen, output.answer, output.item, output.index) == (
        True,
        "Coffee",
        "Coffee",
        2,
    )
    assert _choices(bot) == [("Which?", ["Tea", "Coffee", "Cancel"])]


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_gives_the_whole_thing_that_was_chosen(bot: AsyncMock) -> None:
    books = [Found("Dune (1965)", 1965), Found("Emma (1815)", 1815)]
    bot.request_user_choice.return_value = 0

    output = asyncio.run(
        AskChoice().run(_ctx(), AskChoiceInput(question="Which?", options=books))
    )

    assert output.item is books[0]
    assert output.answer == "Dune (1965)"
    assert _choices(bot)[0][1] == ["Dune (1965)", "Emma (1815)", "Cancel"]


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_shows_some_at_a_time(bot: AsyncMock) -> None:
    options = [f"Book {number}" for number in range(1, 8)]
    # Show more, show more, then the one on the last page.
    bot.request_user_choice.side_effect = [3, 3, 0]

    output = asyncio.run(
        AskChoice().run(
            _ctx(), AskChoiceInput(question="Which?", options=options, at_a_time=3)
        )
    )

    assert (output.answer, output.index) == ("Book 7", 7)
    assert [choices for _, choices in _choices(bot)] == [
        ["Book 1", "Book 2", "Book 3", "Show more", "Cancel"],
        ["Book 4", "Book 5", "Book 6", "Show more", "Cancel"],
        # Nothing more to show after these.
        ["Book 7", "Cancel"],
    ]


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_says_when_nothing_was_chosen(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 2  # Cancel

    output = asyncio.run(
        AskChoice().run(_ctx(), AskChoiceInput(question="Which?", options=["A", "B"]))
    )

    assert (output.chosen, output.answer, output.item, output.index) == (
        False,
        "",
        None,
        0,
    )


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_asks_nothing_when_there_is_nothing_to_choose(
    bot: AsyncMock,
) -> None:
    output = asyncio.run(
        AskChoice().run(_ctx(), AskChoiceInput(question="Which?", options=[]))
    )

    assert output.chosen is False
    bot.request_user_choice.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_can_ask_to_confirm_the_only_option(bot: AsyncMock) -> None:
    inputs = AskChoiceInput(
        question="Which one?", options=[Found("Dune", 1965)], if_one="Is it Dune?"
    )

    bot.request_user_choice.return_value = 0
    yes = asyncio.run(AskChoice().run(_ctx(), inputs))
    assert (yes.chosen, yes.answer, yes.index) == (True, "Dune", 1)
    assert _choices(bot) == [("Is it Dune?", ["Yes", "No"])]

    bot.request_user_choice.return_value = 1
    no = asyncio.run(AskChoice().run(_ctx(), inputs))
    assert no.chosen is False


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_can_lay_options_out_side_by_side(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 2

    output = asyncio.run(
        AskChoice().run(
            _ctx(),
            AskChoiceInput(question="Which?", options=["A", "B"], columns=4),
        )
    )

    # Cancel is in a row of its own, and still counts as the third choice.
    assert _choices(bot) == [("Which?", ["A", "B", ["Cancel"]])]
    assert bot.request_user_choice.await_args.kwargs == {"columns": 4}
    assert output.chosen is False


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_choice_without_a_way_out(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 0

    asyncio.run(
        AskChoice().run(
            _ctx(), AskChoiceInput(question="Which?", options=["A", "B"], cancel="")
        )
    )

    assert _choices(bot) == [("Which?", ["A", "B"])]


def test_the_text_of_an_option() -> None:
    assert option_text("Tea") == "Tea"
    assert option_text({"title": "Dune", "name": "x"}) == "Dune"
    assert option_text(Found("Dune (1965)", 1965)) == "Dune (1965)"
    assert option_text(Found("Dune", 1965), "year") == "1965"
    assert option_text({"id": 7, "name": "Inbox"}) == "Inbox"
    assert option_text(12) == "12"
    with pytest.raises(ValueError, match="no “isbn”"):
        option_text({"title": "Dune"}, "isbn")


def test_ask_choice_refuses_counts_that_mean_nothing() -> None:
    with pytest.raises(ValueError, match="At a time"):
        AskChoiceInput(question="?", options=[], at_a_time=-1)
    with pytest.raises(ValueError, match="Columns"):
        AskChoiceInput(question="?", options=[], columns=0)


# -- picks, one at a time ----------------------------------------------------

HABITS = {"Meditate": "meditate", "Exercise": "exercise", "Read": "read"}


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_each_offers_what_is_left_and_gives_the_value(bot: AsyncMock) -> None:
    block = AskEach()
    inputs = AskEachInput(
        question="Which did you do?", options=HABITS, leave_out=["exercise"]
    )
    bot.request_user_choice.side_effect = [1, 0]

    async def scenario() -> list[Any]:
        first = await block.ask(_ctx(), inputs, [])
        assert first is not None
        second = await block.ask(_ctx(), inputs, [first])
        assert second is not None
        return [first, second]

    first, second = asyncio.run(scenario())

    assert _choices(bot) == [
        ("Which did you do?", ["Meditate", "Read", "Cancel"]),
        ("Which did you do?", ["Meditate", "Cancel"]),
    ]
    assert (first.answer, first.value, first.picked, first.count, first.left) == (
        "Read",
        "read",
        ["read"],
        1,
        1,
    )
    assert (second.value, second.picked, second.count, second.left) == (
        "meditate",
        ["read", "meditate"],
        2,
        0,
    )
    # Nothing is on offer after that, so nothing more is asked.
    assert block.anything_to_ask(inputs, [first]) is True
    assert block.anything_to_ask(inputs, [first, second]) is False
    after = block.after(inputs, [first, second])
    assert (after.picked, after.count, after.left, after.value) == (
        ["read", "meditate"],
        2,
        0,
        "meditate",
    )


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_each_ends_when_the_user_is_done(bot: AsyncMock) -> None:
    inputs = AskEachInput(question="Which?", options=HABITS, done="That is all")
    bot.request_user_choice.return_value = 3

    asked = asyncio.run(AskEach().ask(_ctx(), inputs, []))

    assert asked is None
    assert _choices(bot)[0][1] == ["Meditate", "Exercise", "Read", "That is all"]
    after = AskEach().after(inputs, [])
    assert (after.picked, after.count, after.left, after.answer, after.value) == (
        [],
        0,
        3,
        "",
        None,
    )


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_each_asks_nothing_when_nothing_is_on_offer(bot: AsyncMock) -> None:
    inputs = AskEachInput(
        question="Which?", options=HABITS, leave_out=["meditate", "exercise", "read"]
    )

    assert AskEach().anything_to_ask(inputs, []) is False
    assert asyncio.run(AskEach().ask(_ctx(), inputs, [])) is None
    bot.request_user_choice.assert_not_awaited()


@patch_user_interaction("spanreed.workflows.blocks")
def test_ask_each_puts_the_way_out_in_a_row_of_its_own(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 3

    asyncio.run(
        AskEach().ask(
            _ctx(), AskEachInput(question="Which?", options=HABITS, columns=4), []
        )
    )

    assert _choices(bot) == [("Which?", ["Meditate", "Exercise", "Read", ["Cancel"]])]
    assert bot.request_user_choice.await_args.kwargs == {"columns": 4}


def test_ask_each_needs_a_way_out() -> None:
    with pytest.raises(ValueError, match="way out"):
        AskEachInput(question="?", options={}, done="")
    with pytest.raises(ValueError, match="Columns"):
        AskEachInput(question="?", options={}, columns=0)


# -- the command trigger and the end of a run --------------------------------


def _trigger_ctx() -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=MagicMock(),
        queue=MagicMock(),
    )


def test_a_command_starts_no_run_by_itself() -> None:
    async def scenario() -> None:
        events = Command().events(_trigger_ctx(), NoParams())
        waiting = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.01)
        assert not waiting.done()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


def test_a_command_gives_one_event_for_now_by_hand() -> None:
    now = datetime.datetime(2026, 5, 9, 21, 30, tzinfo=datetime.timezone.utc)
    ctx = _trigger_ctx()

    with patch("spanreed.workflows.asking._now", return_value=now):
        (event,) = asyncio.run(Command().manual_events(ctx, NoParams(), NoQuestion()))

    local = now.astimezone(ctx.user.tzinfo)
    assert (event.date, event.time) == (
        local.date().isoformat(),
        local.strftime("%H:%M"),
    )
    assert Command.Manual is NoQuestion


def test_stop_ends_the_whole_run() -> None:
    with pytest.raises(StopRun) as stopped:
        asyncio.run(Stop().run(_ctx(), StopInput(reason="No books.")))
    assert (stopped.value.reason, stopped.value.whole_run) == ("No books.", True)

    with pytest.raises(StopRun) as plain:
        asyncio.run(Stop().run(_ctx(), StopInput()))
    assert plain.value.reason == "The workflow ends the run here."
