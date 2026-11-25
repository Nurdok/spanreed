"""The blocks that ask the user something, and what goes with them.

A block that asks sets ``converses`` and talks through ``ctx.interaction``.
The run takes the user's turn to talk at its first question, and keeps it
until it ends, so the questions of a run are one conversation. A
workflow's command runs it in the conversation that the command was
started in.

Every block here has the two inputs that say how a run waits:

- ``priority``: ``normal`` waits for its turn behind a conversation that is
  open, and gives way to a command the user starts. ``low`` also gives way
  to any other conversation that starts. ``high`` takes the turn from a
  conversation of normal or low priority, and gives way to nothing.
- ``give_up_after``: the minutes a question waits for the user before the
  run ends. 0 waits for as long as it takes.

A run that gives way ends there, without failing.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Literal

from spanreed.config_form import HELP_KEY, described
from spanreed.workflows.blocks import (
    Action,
    AskLoop,
    Block,
    BlockKind,
    NoOutput,
    NoParams,
    NoQuestion,
    StepContext,
    StopRun,
    Trigger,
    TriggerContext,
)
from spanreed.workflows.builtin import Tick

Priority = Literal["normal", "low", "high"]

YES_NO = ["Yes", "No"]
SHOW_MORE = "Show more"
# Where the text of an option is looked for, if the step does not say.
_LABEL_FIELDS = ("label", "title", "name")


def priority_field() -> Any:
    """Return the ``priority`` input of a block that asks."""
    return field(
        default="normal",
        metadata={
            HELP_KEY: "low waits until no other conversation is open, and "
            "gives way when one starts. high takes the turn from the others"
        },
    )


def give_up_field() -> Any:
    """Return the ``give_up_after`` input of a block that asks."""
    return field(
        default=0,
        metadata={
            HELP_KEY: "Minutes to wait for an answer. 0 waits for as long as it takes"
        },
    )


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock.
    return datetime.datetime.now(datetime.timezone.utc)


class Command(Trigger[NoParams, Tick]):
    """Start a run when the user runs the workflow's command, and only then.

    This is the trigger of a workflow that is a command: it is in the
    command menu of every surface under the workflow's command name.
    """

    id = "core.command"
    title = "When I run its command"
    Manual = NoQuestion

    async def events(
        self, ctx: TriggerContext, params: NoParams
    ) -> AsyncGenerator[Tick, bool | None]:
        """Yield nothing: only the user starts a run."""
        never = asyncio.Event()
        while not await never.wait():
            yield self._tick(ctx)

    async def manual_events(
        self, ctx: TriggerContext, params: NoParams, answer: NoQuestion
    ) -> list[Tick]:
        """Return one event, for now."""
        return [self._tick(ctx)]

    @staticmethod
    def _tick(ctx: TriggerContext) -> Tick:
        local = _now().astimezone(ctx.user.tzinfo)
        return Tick(date=local.date().isoformat(), time=local.strftime("%H:%M"))


@dataclass
class StopInput:
    """The input of ``Stop``."""

    # Shown in the history of the run.
    reason: str = ""


class Stop(Action[StopInput, NoOutput]):
    """End the run here, without failing it.

    Inside a loop this ends the loop as well. ``Only continue if`` ends
    just the round it is in.
    """

    id = "core.stop"
    title = "End the run"
    kind = BlockKind.LOGIC

    async def run(self, ctx: StepContext, inputs: StopInput) -> NoOutput:
        """Raise ``StopRun`` for the whole run."""
        raise StopRun(
            inputs.reason or "The workflow ends the run here.", whole_run=True
        )


@dataclass
class AskTextInput:
    """What ``AskText`` asks."""

    question: str
    ask_first: str = field(
        default="",
        metadata={
            HELP_KEY: "A yes or no question to ask before. On No, the answer is empty"
        },
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class AskTextOutput:
    """What the user wrote."""

    answer: str = described(
        "What you wrote. Empty if you said no to the question before",
    )
    answered: bool = described("Whether you wrote an answer")


class AskText(Action[AskTextInput, AskTextOutput]):
    """Ask the user to write something.

    With ``ask_first``, the user is first asked whether they want to: "Any
    free text to add to the note?". On No, the question is not asked.
    """

    id = "core.ask-text"
    title = "Ask me for text"
    kind = BlockKind.ASK
    converses = True

    async def run(self, ctx: StepContext, inputs: AskTextInput) -> AskTextOutput:
        """Ask, and return the answer."""
        bot = ctx.interaction
        if inputs.ask_first:
            if await bot.request_user_choice(inputs.ask_first, YES_NO) != 0:
                return AskTextOutput(answer="", answered=False)
        answer = (await bot.request_user_input(inputs.question)).strip()
        return AskTextOutput(answer=answer, answered=True)


@dataclass
class AskListInput:
    """What ``AskList`` asks."""

    # Asked before each answer, with Yes and No: Add "Recommended by"?
    add_question: str
    # Asked for each answer: Who recommended it?
    question: str
    add_another_question: str = field(
        default="",
        metadata={
            HELP_KEY: "Asked in place of the first question once there is "
            "an answer. Empty: the same question"
        },
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class AskListOutput:
    """What the user wrote, in order."""

    answers: list[str] = described(
        "What you wrote, one text for each answer, in order",
    )
    count: int = described("How many answers there are")


class AskList(Action[AskListInput, AskListOutput]):
    """Ask the user for texts, one at a time, until they say that was all."""

    id = "core.ask-list"
    title = "Ask me for a list"
    kind = BlockKind.ASK
    converses = True

    async def run(self, ctx: StepContext, inputs: AskListInput) -> AskListOutput:
        """Ask until the user answers No, and return the answers."""
        bot = ctx.interaction
        answers: list[str] = []
        while True:
            prompt = inputs.add_question
            if answers and inputs.add_another_question:
                prompt = inputs.add_another_question
            if await bot.request_user_choice(prompt, YES_NO) != 0:
                break
            answers.append((await bot.request_user_input(inputs.question)).strip())
        return AskListOutput(answers=answers, count=len(answers))


@dataclass
class AskChoiceInput:
    """What ``AskChoice`` asks, and among what."""

    question: str
    # Texts, or things that have a text to show: the books of a search.
    options: list[Any]
    show: str = field(
        default="",
        metadata={
            HELP_KEY: "For options that are not texts: the name of the value "
            "to show. Empty: label, title or name"
        },
    )
    at_a_time: int = field(
        default=0,
        metadata={
            HELP_KEY: "Show this many options at a time, with “Show more”. 0 shows all"
        },
    )
    if_one: str = field(
        default="",
        metadata={
            HELP_KEY: "Asked in place of the question when there is one "
            "option, with Yes and No"
        },
    )
    cancel: str = field(
        default="Cancel",
        metadata={HELP_KEY: "The choice that picks nothing. Empty: there is none"},
    )
    columns: int = field(
        default=1, metadata={HELP_KEY: "How many options go side by side"}
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for counts that mean nothing."""
        if self.at_a_time < 0:
            raise ValueError("“At a time” is 0 or more.")
        if self.columns < 1:
            raise ValueError("“Columns” is 1 or more.")


@dataclass
class AskChoiceOutput:
    """What the user chose."""

    chosen: bool = described(
        "Whether an option was chosen. No if you chose nothing, or if there "
        "was nothing to choose from",
    )
    answer: str = described(
        "The text of the option that was chosen",
        default="",
    )
    item: Any = described(
        "The option as it was given: a text, or the whole thing, such as a " "book",
        default=None,
    )
    index: int = described(
        "The place of the option, from 1. 0 if nothing was chosen",
        default=0,
    )


def option_text(option: Any, show: str = "") -> str:
    """Return the text that stands for an option in a question."""
    if isinstance(option, str):
        return option
    for name in (show,) if show else _LABEL_FIELDS:
        value = (
            option.get(name)
            if isinstance(option, dict)
            else getattr(option, name, None)
        )
        if value is not None and not callable(value):
            return str(value)
    if show:
        raise ValueError(f"An option has no “{show}” to show.")
    return str(option)


class AskChoice(Action[AskChoiceInput, AskChoiceOutput]):
    """Ask the user to choose one of a list of options.

    The options are texts, or things with a text to show, such as what a
    search found: then ``item`` is the whole thing that was chosen. A long
    list can be shown some at a time.

    The step does not end the run if the user chooses nothing. Look at
    ``chosen`` in the steps after it.
    """

    id = "core.ask-choice"
    title = "Ask me to choose"
    kind = BlockKind.ASK
    converses = True

    async def run(self, ctx: StepContext, inputs: AskChoiceInput) -> AskChoiceOutput:
        """Ask, some options at a time, and return the choice."""
        options = inputs.options
        texts = [option_text(option, inputs.show) for option in options]
        if not options:
            return AskChoiceOutput(chosen=False)
        bot = ctx.interaction
        if len(options) == 1 and inputs.if_one:
            if await bot.request_user_choice(inputs.if_one, YES_NO) != 0:
                return AskChoiceOutput(chosen=False)
            return AskChoiceOutput(
                chosen=True, answer=texts[0], item=options[0], index=1
            )

        start = 0
        while True:
            size = inputs.at_a_time or len(options)
            shown = texts[start : start + size]
            more = start + size < len(options)
            extras = ([SHOW_MORE] if more else []) + (
                [inputs.cancel] if inputs.cancel else []
            )
            # The extra choices get a row of their own under several columns.
            choices: list[str | list[str]] = [*shown]
            if extras and inputs.columns == 1:
                choices.extend(extras)
            elif extras:
                choices.append(extras)
            choice = await bot.request_user_choice(
                inputs.question, choices, columns=inputs.columns
            )
            if 0 <= choice < len(shown):
                place = start + choice
                return AskChoiceOutput(
                    chosen=True,
                    answer=texts[place],
                    item=options[place],
                    index=place + 1,
                )
            if more and choice == len(shown):
                start += size
                continue
            return AskChoiceOutput(chosen=False)


@dataclass
class AskEachInput:
    """What ``AskEach`` asks, and among what."""

    question: str
    options: dict[str, Any] = field(
        metadata={HELP_KEY: "What a button says, and the value that picking it gives"}
    )
    leave_out: list[Any] = field(
        default_factory=list,
        metadata={HELP_KEY: "Values that are not offered: the ones done already"},
    )
    done: str = field(
        default="Cancel", metadata={HELP_KEY: "The choice that ends the asking"}
    )
    columns: int = field(
        default=1, metadata={HELP_KEY: "How many options go side by side"}
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for what cannot be asked."""
        if self.columns < 1:
            raise ValueError("“Columns” is 1 or more.")
        if not self.done:
            raise ValueError("“Done” needs a text: it is the way out.")


@dataclass
class AskEachOutput:
    """One pick, for the steps inside. After the loop, all of them."""

    answer: str = described(
        "What the button of the pick said. After the loop: of the last "
        "pick, or empty if there was none",
        default="",
    )
    value: Any = described(
        "The value that the pick gives. After the loop: of the last pick",
        default=None,
    )
    picked: list[Any] = described(
        "Every value picked so far, in order. After the loop: all of them",
        default_factory=list,
    )
    count: int = described(
        "How many were picked so far. After the loop: in all",
        default=0,
    )
    left: int = described(
        "How many options are still on offer",
        default=0,
    )


class AskEach(AskLoop[AskEachInput, AskEachOutput]):
    """Ask the user to pick options, one at a time, until they are done.

    The steps inside run for each pick as it is made, so a pick is acted on
    even if the user never gets to the end. An option that was picked is
    not offered again. Nothing is asked when no option is on offer.
    """

    id = "core.ask-each"
    title = "Ask me to pick, one at a time"
    inside_title = "After each pick"
    round_name = "Pick"

    @staticmethod
    def _on_offer(
        inputs: AskEachInput, earlier: list[AskEachOutput]
    ) -> list[tuple[str, Any]]:
        gone = [*inputs.leave_out, *(pick.value for pick in earlier)]
        return [
            (text, value) for text, value in inputs.options.items() if value not in gone
        ]

    async def ask(
        self, ctx: StepContext, inputs: AskEachInput, earlier: list[AskEachOutput]
    ) -> AskEachOutput | None:
        """Ask for the next pick. Return it, or ``None`` when the asking is over."""
        offered = self._on_offer(inputs, earlier)
        if not offered:
            return None
        choices: list[str | list[str]] = [text for text, _ in offered]
        # The way out gets a row of its own under several columns.
        choices.append(inputs.done if inputs.columns == 1 else [inputs.done])
        choice = await ctx.interaction.request_user_choice(
            inputs.question, choices, columns=inputs.columns
        )
        if not 0 <= choice < len(offered):
            return None
        text, value = offered[choice]
        return AskEachOutput(
            answer=text,
            value=value,
            picked=[*(pick.value for pick in earlier), value],
            count=len(earlier) + 1,
            left=len(offered) - 1,
        )

    def after(self, inputs: AskEachInput, rounds: list[AskEachOutput]) -> AskEachOutput:
        """Return every pick, and how many options were left."""
        last = rounds[-1] if rounds else AskEachOutput()
        return AskEachOutput(
            answer=last.answer,
            value=last.value,
            picked=[pick.value for pick in rounds],
            count=len(rounds),
            left=len(self._on_offer(inputs, rounds)),
        )


def asking_blocks() -> list[Block]:
    """Return the blocks for asking, with what a workflow that asks needs."""
    return [Command(), AskChoice(), AskEach(), AskText(), AskList(), Stop()]
