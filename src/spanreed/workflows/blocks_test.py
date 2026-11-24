import asyncio
import dataclasses
import datetime
import json
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.blocks import (
    SAID_MAX_LINES,
    SAID_MAX_TEXT,
    Interaction,
    Question,
    Repeat,
    StopRun,
    TriggerContext,
    digest,
    manual_questions,
)
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.testing import Event, NoMorePolls, Polled
from spanreed.workflows.values import from_data

NOON = datetime.datetime(2026, 5, 9, 12, 0, tzinfo=datetime.timezone.utc)


class _State:
    """A trigger's state, kept in a dict."""

    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


def _context(state: _State) -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=state,
        queue=WorkflowQueue("wf_1"),
    )


def _poll(
    trigger: Polled,
    state: _State,
    *,
    failures: tuple[str, ...] = (),
    clock: list[Any] | None = None,
) -> list[str]:
    """Run the trigger until its polls run out. Return what it yielded.

    Each value in ``failures`` is one run for that value that fails; every
    other run works. The clock starts at ``clock[0]`` and moves on by the
    time the trigger waits; each wait is added to ``clock``.
    """
    clock = clock if clock is not None else [NOON]
    to_fail = list(failures)

    async def sleep(seconds: float) -> None:
        clock.append(seconds)
        clock[0] += datetime.timedelta(seconds=seconds)

    async def take() -> list[str]:
        yielded = []
        events = trigger.events(_context(state), Repeat(every=1, unit="hours"))
        try:
            event = await anext(events)
            while True:
                yielded.append(event.value)
                fails = event.value in to_fail
                if fails:
                    to_fail.remove(event.value)
                event = await events.asend(not fails)
        except NoMorePolls:
            return yielded

    with (
        patch("spanreed.workflows.blocks._now", new=lambda: clock[0]),
        patch("spanreed.workflows.blocks._sleep", new=sleep),
    ):
        return asyncio.run(take())


def test_repeat_must_wait_between_two_times() -> None:
    assert from_data(Repeat, {"every": 4, "unit": "hours"}).interval == (
        datetime.timedelta(hours=4)
    )
    assert Repeat().interval == datetime.timedelta(hours=1)
    with pytest.raises(ValueError):
        from_data(Repeat, {"every": 0, "unit": "hours"})
    with pytest.raises(ValueError):
        from_data(Repeat, {"every": 4, "unit": "fortnights"})


# -- triggers that poll --------------------------------------------------------


def test_a_poll_gives_what_is_new_since_the_polls_before_it() -> None:
    trigger = Polled(["a", "b"], ["a", "b", "c"], ["c"])

    assert _poll(trigger, _State()) == ["a", "b", "c"]


def test_a_thing_runs_again_when_its_version_changes() -> None:
    trigger = Polled(["day@1"], ["day@1"], ["day@2"], ["day@2"])

    assert _poll(trigger, _State()) == ["day@1", "day@2"]


def test_what_ran_is_remembered_over_a_restart() -> None:
    state = _State()

    assert _poll(Polled(["a", "b"]), state) == ["a", "b"]
    assert _poll(Polled(["a", "b", "c"]), state) == ["c"]


def test_a_thing_whose_run_failed_is_given_again_by_the_next_poll() -> None:
    trigger = Polled(["a", "b"], ["a", "b"], ["a", "b"])
    state = _State()

    # "a" fails in the first two polls. "b" is not held up by it.
    assert _poll(trigger, state, failures=("a", "a")) == ["a", "b", "a", "a"]
    assert json.loads(state.values["seen"]) == {"b": "", "a": ""}


def test_the_cursor_moves_on_when_every_run_of_a_poll_worked() -> None:
    trigger = Polled(["a"], ["b", "c"], ["b"], [])
    state = _State()

    # The run for "b" fails in the second poll.
    assert _poll(trigger, state, failures=("b",)) == ["a", "b", "c", "b"]

    # So the second poll kept the cursor of the first, and the third poll
    # started where the second did.
    assert trigger.cursors == [None, "after poll 1", "after poll 1", "after poll 3"]
    assert state.values["cursor"] == "after poll 4"


def test_the_first_poll_is_at_once_and_the_next_an_interval_later() -> None:
    clock: list[Any] = [NOON]

    _poll(Polled(["a"], [], []), _State(), clock=clock)

    # No wait before the first poll. One hour before each of the others,
    # and before the poll that the test ends on.
    assert clock[1:] == [3600, 3600, 3600]


def test_polling_keeps_its_pace_over_a_restart() -> None:
    # Polled 20 minutes before the restart: 40 minutes of the hour are left.
    polled_at = (NOON - datetime.timedelta(minutes=20)).isoformat()
    clock: list[Any] = [NOON]

    _poll(Polled(["a"]), _State(polled_at=polled_at), clock=clock)

    assert clock[1] == 40 * 60


def test_a_poll_that_is_due_after_a_restart_happens_at_once() -> None:
    polled_at = (NOON - datetime.timedelta(hours=5)).isoformat()
    clock: list[Any] = [NOON]

    assert _poll(Polled(["a"]), _State(polled_at=polled_at), clock=clock) == ["a"]
    # The only wait is the one before the poll that the test ends on.
    assert clock[1:] == [3600]


def test_the_time_of_a_poll_is_kept_when_its_runs_are_over() -> None:
    state = _State()
    trigger = Polled(["a"])

    async def take_one() -> None:
        events = trigger.events(_context(state), Repeat())
        await anext(events)
        # The run for "a" is still going: a restart now polls again.
        assert "polled_at" not in state.values
        await events.aclose()

    asyncio.run(take_one())


def test_only_the_last_keys_that_ran_are_remembered() -> None:
    class Forgetful(Polled):
        remembers = 2

    state = _State()

    _poll(Forgetful(["a", "b"], ["a", "c"]), state)

    # "a" was not new in the second poll, so it is still the oldest.
    assert list(json.loads(state.values["seen"])) == ["b", "c"]


def test_state_that_cannot_be_read_starts_over() -> None:
    state = _State(seen="not json", polled_at="not a time")

    assert _poll(Polled(["a"]), state) == ["a"]
    assert json.loads(state.values["seen"]) == {"a": ""}


def test_a_digest_changes_with_any_field_of_the_event() -> None:
    assert digest(Event("a")) == digest(Event("a"))
    assert digest(Event("a")) != digest(Event("b"))
    assert len(digest(Event("a"))) == 16


# -- what a manual run asks ----------------------------------------------------


@dataclasses.dataclass
class _Ask:
    which: Literal["All", "One"] = dataclasses.field(metadata={"prompt": "Which?"})
    name: str = dataclasses.field(default="", metadata={"ask_if": {"which": "One"}})


def test_a_manual_run_can_ask_for_choices_and_for_texts() -> None:
    assert manual_questions(_Ask) == [
        Question(name="which", prompt="Which?", choices=["All", "One"], ask_if={}),
        Question(name="name", prompt=None, choices=None, ask_if={"which": "One"}),
    ]


def _manual(**fields: Any) -> type:
    return dataclasses.make_dataclass("Manual", list(fields.items()))


def test_a_question_is_a_choice_of_texts_or_a_text() -> None:
    with pytest.raises(ValueError, match="must be a Literal of strings, or str"):
        manual_questions(_manual(days=int))
    with pytest.raises(ValueError, match="must be a Literal of strings"):
        manual_questions(_manual(days=Literal[1, 7]))


def test_a_question_can_only_depend_on_an_earlier_one() -> None:
    later = dataclasses.field(default="", metadata={"ask_if": {"other": "x"}})
    with pytest.raises(ValueError, match="ask_if must name earlier fields"):
        manual_questions(dataclasses.make_dataclass("Manual", [("name", str, later)]))


def test_a_question_that_is_not_always_asked_needs_a_default() -> None:
    sometimes = dataclasses.field(metadata={"ask_if": {"which": "One"}})
    with pytest.raises(ValueError, match="needs a default"):
        manual_questions(
            dataclasses.make_dataclass(
                "Manual", [("which", Literal["All", "One"]), ("name", str, sometimes)]
            )
        )


# -- what a block talks to the user through ----------------------------------


@patch_user_interaction("spanreed.workflows.blocks")
def test_a_question_takes_the_turn_first_and_sending_does_not(bot: AsyncMock) -> None:
    order: list[str] = []

    async def choose(*args: Any, **kwargs: Any) -> int:
        order.append("asked")
        return 1

    async def write(*args: Any, **kwargs: Any) -> str:
        order.append("asked")
        return "x"

    bot.request_user_choice.side_effect = choose
    bot.request_user_input.side_effect = write

    async def take() -> None:
        order.append("turn")

    async def scenario() -> Interaction:
        talk = Interaction(mock_user_find_by_id(4))
        said: list[dict[str, str]] = []
        talk.keep_in(said)
        talk.asks_with(take)
        assert talk.talked is False
        await talk.send_message("Hello")
        await talk.send_document("a.pdf", b"x")
        assert order == []
        choices: list[str | list[str]] = ["A", ["B", "C"]]
        assert await talk.request_user_choice("Which?", choices) == 1
        assert await talk.request_user_input("Why?") == "x"
        assert said == [
            {"said": "Hello"},
            {"sent": "a.pdf"},
            # A choice in a row of its own counts in the order it is read.
            {"asked": "Which?", "answer": "B"},
            {"asked": "Why?", "answer": "x"},
        ]
        return talk

    talk = asyncio.run(scenario())

    assert order == ["turn", "asked", "turn", "asked"]
    assert talk.talked is True


@patch_user_interaction("spanreed.workflows.blocks")
def test_a_question_without_an_answer_in_time_ends_the_run(bot: AsyncMock) -> None:
    async def never(*args: Any, **kwargs: Any) -> str:
        await asyncio.Event().wait()
        return ""

    bot.request_user_input.side_effect = never

    async def scenario() -> None:
        talk = Interaction(mock_user_find_by_id(4))
        talk.asks_with(None, 0.01, "There was no answer for 1 minute.")
        await talk.request_user_input("Why?")

    with pytest.raises(StopRun) as stopped:
        asyncio.run(scenario())
    assert stopped.value.reason == "There was no answer for 1 minute."
    assert stopped.value.whole_run is True


@patch_user_interaction("spanreed.workflows.blocks")
def test_another_timeout_inside_a_question_is_not_the_user_being_slow(
    bot: AsyncMock,
) -> None:
    bot.request_user_input.side_effect = TimeoutError("the surface is gone")

    async def scenario() -> None:
        talk = Interaction(mock_user_find_by_id(4))
        talk.asks_with(None, 60, "There was no answer.")
        await talk.request_user_input("Why?")

    with pytest.raises(TimeoutError, match="the surface is gone"):
        asyncio.run(scenario())


@patch_user_interaction("spanreed.workflows.blocks")
def test_the_trace_keeps_the_start_of_a_long_conversation(bot: AsyncMock) -> None:
    async def scenario() -> list[dict[str, str]]:
        talk = Interaction(mock_user_find_by_id(4))
        said: list[dict[str, str]] = []
        talk.keep_in(said)
        await talk.send_message("x" * (SAID_MAX_TEXT + 10))
        for number in range(SAID_MAX_LINES + 5):
            await talk.send_message(str(number))
        return said

    said = asyncio.run(scenario())

    assert len(said) == SAID_MAX_LINES
    assert said[0]["said"] == "x" * SAID_MAX_TEXT + "…"
