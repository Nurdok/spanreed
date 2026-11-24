"""The blocks that workflows are made of, and the registry that holds them.

A block has an id, a title, and typed inputs and outputs. The inputs and
outputs are dataclasses, so a form and a check of the values come from the
type hints, as for a plugin config (see ``spanreed.config_form``).

There are two shapes of block:

- An ``Action`` is a step. It gets its inputs and returns its outputs.
  Blocks that ask the user something and blocks that hold logic are actions
  too; ``kind`` says which, for the editor. A ``Loop`` is an action that
  holds steps, and runs them once for each of its rounds. A ``Branch``
  holds two lists of steps, and runs one of them. An action that
  ``converses`` holds a conversation with the user, through
  ``StepContext.interaction``. An ``AskLoop`` is a loop that asks for each
  of its rounds.
- A ``Trigger`` starts runs. It yields one event for each run, and says
  whether, and how, it can be started by hand. A ``PushTrigger`` is a
  trigger that a plugin feeds: the plugin says what changed, and the
  trigger turns each change into an event. A ``PollTrigger`` asks a
  service at an interval, and remembers what the workflow already ran for.

A plugin publishes its blocks from ``Plugin.blocks()``.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
import datetime
import enum
import hashlib
import json
import re
import typing
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    cast,
)

from spanreed.interaction import UserInteractionApi
from spanreed.interaction.surface import MessageHandle
from spanreed.user import User

if TYPE_CHECKING:
    from spanreed.plugin import Plugin

InT = TypeVar("InT")
OutT = TypeVar("OutT")
ParamsT = TypeVar("ParamsT")
EventT = TypeVar("EventT")
RepeatT = TypeVar("RepeatT", bound="Repeat")

_BLOCK_ID_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")

# Indirection so that tests can skip the wait between two polls.
_sleep = asyncio.sleep


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock of a poll trigger.
    return datetime.datetime.now(datetime.timezone.utc)


class BlockKind(enum.StrEnum):
    """What a block is for. The editor groups and colors blocks by it."""

    TRIGGER = "trigger"
    ACTION = "action"
    ASK = "ask"
    LOGIC = "logic"


class ManualError(Exception):
    """Raised by a trigger that cannot give the events of a manual run.

    The message is for the user: the answer was not a date, or there is
    nothing of that name.
    """


class StopRun(Exception):
    """Raised by a block to end the run at this step without failing it.

    In a loop, that ends the round the step is in, and the loop goes on
    with the next one. ``whole_run`` ends the loop as well, and the run:
    the user cancelled, or the run lost its turn to talk.
    """

    def __init__(self, reason: str, *, whole_run: bool = False) -> None:
        """Create the signal. ``reason`` is shown in the run's trace."""
        super().__init__(reason)
        self.reason = reason
        self.whole_run = whole_run


@dataclass
class NoInput:
    """The inputs of a block that has none."""


@dataclass
class NoOutput:
    """The outputs of a block that has none."""


@dataclass
class NoParams:
    """The parameters of a trigger that has none."""


@dataclass
class NoQuestion:
    """The question of a trigger that a manual run starts without asking."""


_UNITS = {
    "minutes": datetime.timedelta(minutes=1),
    "hours": datetime.timedelta(hours=1),
    "days": datetime.timedelta(days=1),
}


@dataclass
class Repeat:
    """How often a trigger does its work: every 4 hours.

    These are the parameters of a schedule and of a trigger that polls.
    """

    every: int = 1
    unit: Literal["minutes", "hours", "days"] = "hours"

    def __post_init__(self) -> None:
        """Raise ``ValueError`` if there would be no wait in between."""
        if self.every < 1:
            raise ValueError("A trigger repeats every 1 or more.")

    @property
    def interval(self) -> datetime.timedelta:
        """Return the time from one time to the next."""
        return self.every * _UNITS[self.unit]


@dataclass(frozen=True)
class RunInfo:
    """What a step can know about the run it is part of.

    Step inputs can refer to it as ``run``: ``{{ run.manual }}``.
    """

    id: str
    # True if the user started the run by hand.
    manual: bool


# How much of one text of a conversation the trace keeps, and how many
# questions, answers and messages of one step.
SAID_MAX_TEXT = 500
SAID_MAX_LINES = 50


def _said(text: str) -> str:
    return text if len(text) <= SAID_MAX_TEXT else text[:SAID_MAX_TEXT] + "…"


class Interaction:
    """What a block that converses talks to the user through.

    It has the methods of ``UserInteractionApi`` that a conversation is
    made of, with the same arguments, so the body of a plugin's
    conversation moves into a block as it is. What is asked, answered and
    said is kept as well, for the trace of the step.

    The engine has taken the user's turn to talk before it calls a block
    that ``converses``. A block must not take it again. A block that only
    sends something, and asks nothing, needs no turn: it sends through
    this without setting ``converses``.
    """

    def __init__(self, user: User) -> None:
        """Create the way to talk to ``user``."""
        self._user = user
        self._lines: list[dict[str, str]] = []
        self._talked = False

    @property
    def talked(self) -> bool:
        """Return whether the user was asked or told anything through this."""
        return self._talked

    def keep_in(self, lines: list[dict[str, str]]) -> None:
        """Keep what is asked and said from now on in ``lines``."""
        self._lines = lines

    def _keep(self, **line: str) -> None:
        self._talked = True
        if len(self._lines) < SAID_MAX_LINES:
            self._lines.append({name: _said(text) for name, text in line.items()})

    async def request_user_choice(
        self,
        prompt: str,
        choices: list[str] | list[str | list[str]],
        *,
        columns: int = 1,
    ) -> int:
        """Ask the user to pick one of ``choices``. Return its place, from 0.

        A choice that is a list is a row of its own, and its texts count
        as choices in the order they are read.
        """
        bot = await UserInteractionApi.for_user(self._user)
        choice = await bot.request_user_choice(prompt, choices, columns=columns)
        texts: list[str] = []
        for item in choices:
            texts.extend(item if isinstance(item, list) else [item])
        answer = texts[choice] if 0 <= choice < len(texts) else str(choice)
        self._keep(asked=prompt, answer=answer)
        return choice

    async def request_user_input(self, prompt: str) -> str:
        """Ask the user for a text, and return it."""
        bot = await UserInteractionApi.for_user(self._user)
        answer = await bot.request_user_input(prompt)
        self._keep(asked=prompt, answer=answer)
        return answer

    async def send_message(
        self, text: str, *, parse_html: bool = True, parse_markdown: bool = False
    ) -> MessageHandle:
        """Send the user a message, on the surfaces of the conversation."""
        bot = await UserInteractionApi.for_user(self._user)
        handle = await bot.send_message(
            text, parse_html=parse_html, parse_markdown=parse_markdown
        )
        self._keep(said=text)
        return handle

    async def send_document(self, file_name: str, data: bytes) -> MessageHandle:
        """Send the user a file, on the surfaces of the conversation."""
        bot = await UserInteractionApi.for_user(self._user)
        handle = await bot.send_document(file_name, data)
        self._keep(sent=file_name)
        return handle


@dataclass(frozen=True)
class StepContext:
    """What the engine hands to an action besides its inputs."""

    user: User
    workflow_id: str
    run: RunInfo
    # For a block that ``converses``. One for the whole run.
    interaction: Interaction = dataclasses.field(default=cast(Interaction, None))

    def __post_init__(self) -> None:
        """Make the way to talk to the user, if none was given."""
        if self.interaction is None:
            object.__setattr__(self, "interaction", Interaction(self.user))


class TriggerState(Protocol):
    """Small values a trigger keeps between events, such as a cursor.

    They are kept for each workflow and survive a restart.
    """

    async def get(self, name: str) -> str | None:
        """Return a stored value, or ``None`` if it is not set."""

    async def set(self, name: str, value: str) -> None:
        """Store a value."""


class EventQueue(Protocol):
    """The keys of what a workflow still has to run for, oldest first.

    A key names something that changed: a date, a record. It is kept for
    each workflow and survives a restart. A key stays in the queue while
    the workflow runs for it, so a run that a restart cuts short happens
    again.
    """

    async def put(self, key: str) -> None:
        """Add a key, or note that a key that waits changed again."""

    async def next(self) -> tuple[str, str]:
        """Wait for the oldest key. Return it with a token for ``done``."""

    async def done(self, key: str, token: str) -> None:
        """Take a key out, unless it was put again since ``next`` gave it."""

    async def later(self, key: str) -> None:
        """Move a key behind the others, to run for it again later."""


@dataclass(frozen=True)
class TriggerContext:
    """What the engine hands to a trigger besides its parameters."""

    user: User
    workflow_id: str
    state: TriggerState
    queue: EventQueue


def _type_arguments(cls: type, generic: type) -> tuple[Any, ...] | None:
    """Return the types a class gave to its generic base, if it gave any.

    The base can be ``generic`` itself or a generic class between the two,
    such as ``PushTrigger``. A class that passes type variables on has not
    given any types yet.
    """
    for base in getattr(cls, "__orig_bases__", ()):
        origin = typing.get_origin(base)
        if not (isinstance(origin, type) and issubclass(origin, generic)):
            continue
        arguments = typing.get_args(base)
        if not any(isinstance(argument, TypeVar) for argument in arguments):
            return arguments
    return None


class Block(abc.ABC):
    """Base of every block."""

    # Stable identity, ``<plugin>.<name>``. Stored workflows refer to it, so
    # it must not change when the title does.
    id: ClassVar[str]
    title: ClassVar[str]
    kind: ClassVar[BlockKind]


class Action(Block, Generic[InT, OutT]):
    """A step: ``run`` gets the inputs and returns the outputs.

    Name the two dataclasses as the type arguments::

        class FindTasks(Action[FindTasksInput, FindTasksOutput]):
            ...

    Raise ``StopRun`` to end the run here without failing it. Any other
    exception fails the run.

    A block that asks the user something sets ``converses``, and talks
    through ``ctx.interaction``. Before the engine calls such a block, the
    run takes the user's turn to talk, and it keeps the turn until it
    ends. Two inputs say how, if the block has them (see
    ``spanreed.workflows.asking`` for their types): ``priority``, how the
    run waits for the turn, and ``give_up_after``, for how many minutes
    the step waits for the user.
    """

    kind = BlockKind.ACTION
    Input: ClassVar[type[Any]]
    Output: ClassVar[type[Any]]
    converses: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Take ``Input`` and ``Output`` from the type arguments."""
        super().__init_subclass__(**kwargs)
        arguments = _type_arguments(cls, Action)
        if arguments is not None:
            cls.Input, cls.Output = arguments

    @abc.abstractmethod
    async def run(self, ctx: StepContext, inputs: InT) -> OutT:
        """Do the step for one run."""

    def lanes(self) -> list[tuple[str, str]]:
        """Return the lists of steps that a step of this block holds.

        Each is the name of the list in a step, ``steps`` or ``otherwise``,
        and the title it is shown under. None for a plain action.
        """
        return []

    def writes(self, inputs: dict[str, Any]) -> list[str]:
        """Name what a step of this block writes that another could too.

        ``inputs`` are the step's inputs as they are stored, templates and
        all. Each name ends a sentence that starts with "also writes":
        ``the property “weight” of Daily/{{ trigger.date }}.md``. Two steps
        that give the same name write the same thing, and the editor says
        so when they are in two workflows. Nothing by default.
        """
        return []


class Loop(Action[InT, OutT]):
    """An action that holds steps, and runs them once for each round.

    The engine runs the steps inside for each value that ``rounds``
    returns. Inside, the id of the loop's step refers to that value, so a
    step of ``For each`` whose id is ``each`` reads the item it runs for as
    ``{{ each.item }}``. After the loop, the id refers to what ``run``
    returns, as for any action. Both have the type ``Output``.

    A step inside that ends the run (``StopRun``) ends only its round. A
    step inside that fails makes the loop fail, and with it the run.
    """

    kind = BlockKind.LOGIC
    # What the steps inside are shown under, and what one time through
    # them is called.
    inside_title: ClassVar[str] = "For each round"
    round_name: ClassVar[str] = "Round"

    def lanes(self) -> list[tuple[str, str]]:
        """Return the one list of steps of a loop."""
        return [("steps", self.inside_title)]

    @abc.abstractmethod
    def rounds(self, inputs: InT) -> list[OutT]:
        """Return what the steps inside see, for each time they run."""


class AskLoop(Loop[InT, OutT]):
    """A loop whose rounds are what the user answers, one answer at a time.

    The engine calls ``ask`` before each round, with the values of the
    rounds before it. ``ask`` asks the user through ``ctx.interaction`` and
    returns the value of the next round, or ``None`` when there are no
    more: the user is done, or there is nothing left to ask about. The
    steps inside run for an answer before the next question is asked, so
    they can act on each answer as it comes.

    After the loop, the id of the step refers to what ``after`` returns
    for the rounds there were.
    """

    kind = BlockKind.ASK
    converses = True
    inside_title: ClassVar[str] = "After each answer"
    round_name: ClassVar[str] = "Answer"

    def rounds(self, inputs: InT) -> list[OutT]:
        """Return nothing: the rounds are not known before they are asked for."""
        return []

    def anything_to_ask(self, inputs: InT, earlier: list[OutT]) -> bool:
        """Return whether ``ask`` would ask the user anything now.

        The engine ends the loop without calling ``ask`` if not, so that a
        run does not wait for the user's turn to talk and then say nothing.
        Yes by default.
        """
        return True

    @abc.abstractmethod
    async def ask(
        self, ctx: StepContext, inputs: InT, earlier: list[OutT]
    ) -> OutT | None:
        """Ask for the next round. Return its value, or ``None`` to end the loop."""

    @abc.abstractmethod
    def after(self, inputs: InT, rounds: list[OutT]) -> OutT:
        """Return what the steps after the loop see, for the rounds there were."""

    async def run(self, ctx: StepContext, inputs: InT) -> OutT:
        """Return what ``after`` gives for no rounds. The engine does not call this."""
        return self.after(inputs, [])


class Branch(Action[InT, OutT]):
    """An action that holds two lists of steps, and runs one of them.

    ``takes_first`` says which, from the inputs: the step's ``steps``, or
    its ``otherwise``. The steps inside can refer to the branch by its id
    for what ``run`` returns, as the steps after it can. A step after the
    branch cannot refer to a step inside it: only one side ran.

    A step inside that ends the run (``StopRun``) ends it, or ends the
    round of the loop that the branch is in. A step inside that fails
    makes the branch fail.
    """

    kind = BlockKind.LOGIC
    # What the two lists of steps are shown under.
    first_title: ClassVar[str] = "Then"
    second_title: ClassVar[str] = "Otherwise"

    def lanes(self) -> list[tuple[str, str]]:
        """Return the two lists of steps of a branch."""
        return [("steps", self.first_title), ("otherwise", self.second_title)]

    @abc.abstractmethod
    def takes_first(self, inputs: InT) -> bool:
        """Return whether the first list of steps runs, and not the second."""


class Trigger(Block, Generic[ParamsT, EventT]):
    """What starts a run. Each event that ``events`` yields is one run.

    Name the parameters and the event as the type arguments. The fields of
    the event are the trigger's outputs: steps refer to them as
    ``{{ trigger.<field> }}``.

    ``Manual`` says whether the user can start a run by hand. ``None`` means
    no. Otherwise it is a dataclass that holds what to ask first (see
    ``manual_questions``), and ``NoQuestion`` asks nothing. A trigger with
    a ``Manual`` implements ``manual_events``.
    """

    kind = BlockKind.TRIGGER
    Params: ClassVar[type[Any]]
    Event: ClassVar[type[Any]]
    Manual: ClassVar[type[Any] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Take ``Params`` and ``Event`` from the type arguments."""
        super().__init_subclass__(**kwargs)
        arguments = _type_arguments(cls, Trigger)
        if arguments is not None:
            cls.Params, cls.Event = arguments

    @abc.abstractmethod
    def events(
        self, ctx: TriggerContext, params: ParamsT
    ) -> AsyncGenerator[EventT, bool | None]:
        """Yield an event each time the workflow should run.

        The engine runs the workflow for an event before it asks for the
        next one, so code after a ``yield`` runs when that run is over. The
        ``yield`` gives back whether the run worked: ``False`` if it failed
        every time it was tried. The engine cancels the generator when the
        workflow is turned off, and starts it again, after a pause, if it
        raises.
        """

    async def manual_events(
        self, ctx: TriggerContext, params: ParamsT, answer: Any
    ) -> list[EventT]:
        """Return the events of a run started by hand.

        ``answer`` is an instance of ``Manual``, filled in by the user.
        Raise ``ManualError`` to tell the user why there is nothing to run.
        """
        raise NotImplementedError(f"{self.id} cannot be started by hand.")

    async def manual_run_done(
        self, ctx: TriggerContext, params: ParamsT, event: EventT, worked: bool
    ) -> None:
        """Take note that a run started by hand is over.

        ``event`` is one that ``manual_events`` returned. Nothing is done
        by default.
        """


class PushTrigger(Trigger[ParamsT, EventT]):
    """A trigger that its plugin feeds with the keys of what changed.

    The plugin calls ``emit`` with a key for each thing that changed, for
    example a date. The key waits in the queue of every workflow that uses
    the trigger. ``event_for`` turns a key into an event when the workflow
    is ready to run for it, so the event holds the state of that moment and
    a key that is put twice runs once.

    A key leaves the queue when its run worked. If the run failed every
    time it was tried, the key goes behind the others and is tried again
    after them.
    """

    @classmethod
    async def emit(cls, user: User, keys: Iterable[str]) -> None:
        """Say that these keys changed for a user."""
        # Imported here: the store imports the model, which imports this.
        from spanreed.workflows.store import emit

        await emit(user.id, cls.id, keys)

    async def events(
        self, ctx: TriggerContext, params: ParamsT
    ) -> AsyncGenerator[EventT, bool | None]:
        """Yield the event of each key in the queue, oldest first."""
        while True:
            key, token = await ctx.queue.next()
            event = await self.event_for(ctx, params, key)
            worked = True if event is None else (yield event)
            if worked is False:
                await ctx.queue.later(key)
            else:
                await ctx.queue.done(key, token)

    @abc.abstractmethod
    async def event_for(
        self, ctx: TriggerContext, params: ParamsT, key: str
    ) -> EventT | None:
        """Return the event for a key, or ``None`` if there is nothing to run."""


def digest(event: Any) -> str:
    """Return a short text that differs when any field of an event does.

    It is what ``PollTrigger.version`` returns for a trigger that runs
    again when the thing an event is about has changed.
    """
    data = json.dumps(dataclasses.asdict(event), sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


@dataclass
class Poll(Generic[EventT]):
    """What a poll found."""

    # An event for each thing the service has now, oldest first. They may
    # include what earlier polls found: the trigger leaves those out.
    events: list[EventT]
    # Where the next poll starts, for a service that can be asked for what
    # is new since a point. It is kept once every run of this poll worked.
    # ``None`` keeps the cursor as it is.
    cursor: str | None = None


_POLLED_AT = "polled_at"
_CURSOR = "cursor"
_SEEN = "seen"


class PollTrigger(Trigger[RepeatT, EventT]):
    """A trigger that asks a service, at an interval, what it has.

    ``poll`` returns an event for each thing it finds. The trigger keeps
    what the workflow ran for, so that only something new starts a run:
    ``key`` names the thing an event is about, and ``version`` says what
    state the thing is in. By default a key runs once. A trigger whose
    ``version`` is ``digest(event)`` also runs when the thing has changed.

    An event is remembered when its run worked. If the run failed every
    time it was tried, the next poll finds the event again, so a poll must
    return it until then: by looking back far enough, or through the
    cursor, which only moves on when every run of a poll worked.

    The parameters say how often to ask. They are ``Repeat``, or a
    dataclass that adds fields to it.
    """

    # How many keys are remembered. The ones that ran longest ago go first.
    remembers: ClassVar[int] = 1000

    @abc.abstractmethod
    async def poll(
        self, ctx: TriggerContext, params: RepeatT, cursor: str | None
    ) -> Poll[EventT]:
        """Ask the service, and return what it has.

        ``cursor`` is what the last poll returned as its cursor, or
        ``None`` if no poll has kept one.
        """

    @abc.abstractmethod
    def key(self, event: EventT) -> str:
        """Return what names the thing an event is about, such as its id."""

    def version(self, event: EventT) -> str:
        """Return what tells one state of the thing from another.

        The workflow runs for a key again when this differs from the last
        time it ran for the key.
        """
        return ""

    async def events(
        self, ctx: TriggerContext, params: RepeatT
    ) -> AsyncGenerator[EventT, bool | None]:
        """Poll each time the interval has passed, and yield what is new."""
        while True:
            await self._wait_until_due(ctx, params)
            polled_at = _now()
            found = await self.poll(ctx, params, await ctx.state.get(_CURSOR))
            every_run_worked = True
            for event in found.events:
                # Read each time: a run by hand may have been for this event.
                if not self._is_new(event, await self._seen(ctx)):
                    continue
                worked = yield event
                if worked is False:
                    every_run_worked = False
                else:
                    await self._remember(ctx, event)
            if every_run_worked and found.cursor is not None:
                await ctx.state.set(_CURSOR, found.cursor)
            # Kept only now, when the runs are over: a poll that a restart
            # cut short is due again when Spanreed comes back.
            await ctx.state.set(_POLLED_AT, polled_at.isoformat())

    async def new_events(self, ctx: TriggerContext, params: RepeatT) -> list[EventT]:
        """Poll now, and return what the workflow has not run for.

        For ``manual_events`` of a trigger that lets the user check now.
        The cursor stays where it is, and the pace of the polls too.
        """
        found = await self.poll(ctx, params, await ctx.state.get(_CURSOR))
        seen = await self._seen(ctx)
        return [event for event in found.events if self._is_new(event, seen)]

    async def manual_run_done(
        self, ctx: TriggerContext, params: RepeatT, event: EventT, worked: bool
    ) -> None:
        """Remember an event that a run by hand worked for.

        So that the next poll does not run for it again.
        """
        if worked:
            await self._remember(ctx, event)

    @staticmethod
    async def start_from(
        state: TriggerState, *, cursor: str | None, keys: Iterable[str]
    ) -> None:
        """Give a trigger that never polled the memory of what was done.

        For a plugin that becomes a workflow: ``cursor`` is where the
        plugin's own polls had got to, and ``keys`` are the things it
        already handled, which the workflow must not run for again. They
        are remembered as a trigger that runs once for a key does.

        Do nothing if the trigger has a cursor or a memory already.
        """
        if await state.get(_CURSOR) is not None or await state.get(_SEEN) is not None:
            return
        if cursor is not None:
            await state.set(_CURSOR, cursor)
        seen = {key: "" for key in keys}
        if seen:
            await state.set(_SEEN, json.dumps(seen))

    def _is_new(self, event: EventT, seen: dict[str, str]) -> bool:
        return seen.get(self.key(event)) != self.version(event)

    async def _remember(self, ctx: TriggerContext, event: EventT) -> None:
        """Keep that the workflow ran for an event."""
        seen = await self._seen(ctx)
        # Put last, so that the keys stay in the order they ran in.
        seen.pop(self.key(event), None)
        seen[self.key(event)] = self.version(event)
        while len(seen) > self.remembers:
            del seen[next(iter(seen))]
        await ctx.state.set(_SEEN, json.dumps(seen))

    @staticmethod
    async def _wait_until_due(ctx: TriggerContext, params: Repeat) -> None:
        """Wait for the rest of the interval since the last poll."""
        stored = await ctx.state.get(_POLLED_AT)
        if stored is None:
            return
        try:
            last = datetime.datetime.fromisoformat(stored)
        except ValueError:
            return
        wait = (last + params.interval - _now()).total_seconds()
        if wait > 0:
            await _sleep(wait)

    @staticmethod
    async def _seen(ctx: TriggerContext) -> dict[str, str]:
        """Return the version that the workflow last ran for, by key."""
        stored = await ctx.state.get(_SEEN)
        if stored is None:
            return {}
        try:
            seen = json.loads(stored)
        except ValueError:
            return {}
        return seen if isinstance(seen, dict) else {}


class BlockRegistry:
    """Every block that the loaded plugins publish, by id."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._blocks: dict[str, Block] = {}
        self._plugins: dict[str, Plugin] = {}

    @classmethod
    def from_plugins(cls, plugins: Iterable[Plugin]) -> BlockRegistry:
        """Return a registry of the blocks of ``plugins``."""
        registry = cls()
        for plugin in plugins:
            for block in plugin.blocks():
                registry.register(block, plugin)
        return registry

    def register(self, block: Block, plugin: Plugin) -> None:
        """Add a block that ``plugin`` publishes.

        Raise ``ValueError`` if the block is not well formed, or if a block
        with the same id is registered already.
        """
        if not _BLOCK_ID_RE.match(getattr(block, "id", "")):
            raise ValueError(
                f"{type(block).__name__}: a block id looks like 'plugin.name'."
            )
        if block.id in self._blocks:
            raise ValueError(f"Block {block.id} is already registered.")
        if isinstance(block, Action):
            _require_dataclasses(block, "Input", "Output")
        elif isinstance(block, Trigger):
            _require_dataclasses(block, "Params", "Event")
            _check_manual(block)
        else:
            raise ValueError(f"Block {block.id} is not an Action or a Trigger.")
        self._blocks[block.id] = block
        self._plugins[block.id] = plugin

    def get(self, block_id: str) -> Block | None:
        """Return a block, or ``None`` if no plugin publishes it."""
        return self._blocks.get(block_id)

    def plugin_of(self, block_id: str) -> Plugin:
        """Return the plugin that publishes a registered block."""
        return self._plugins[block_id]

    def all(self) -> list[Block]:
        """Return every block, in the order the plugins published them."""
        return list(self._blocks.values())


def _require_dataclasses(block: Block, *names: str) -> None:
    for name in names:
        tp = getattr(block, name, None)
        if not (isinstance(tp, type) and dataclasses.is_dataclass(tp)):
            raise ValueError(f"Block {block.id}: {name} must be a dataclass.")


@dataclass(frozen=True)
class Question:
    """One thing that a manual run asks the user first."""

    # The field of ``Manual`` that the answer goes to.
    name: str
    # What to ask. ``None`` leaves the wording to the engine.
    prompt: str | None
    # The answers to pick from. ``None`` asks for a text.
    choices: list[str] | None
    # Asked only if the earlier answers are these, by field name.
    ask_if: dict[str, str]


def manual_questions(manual: type[Any]) -> list[Question]:
    """Return what a ``Manual`` asks, in the order of its fields.

    A field that is a ``Literal`` of strings is a choice, and a field that
    is a ``str`` is a text. The metadata of a field can hold the ``prompt``
    and ``ask_if``: the answers to earlier questions that this one depends
    on. A question that is not asked leaves its field at its default.

    Raise ``ValueError`` if a field is of another type, if ``ask_if``
    names a field that is not an earlier one, or if a field that is not
    always asked has no default.
    """
    hints = typing.get_type_hints(manual)
    questions: list[Question] = []
    for field in dataclasses.fields(manual):
        where = f"{manual.__qualname__}.{field.name}"
        tp = hints[field.name]
        choices: list[str] | None = None
        if typing.get_origin(tp) is typing.Literal:
            choices = list(typing.get_args(tp))
            if not all(isinstance(choice, str) for choice in choices):
                raise ValueError(f"{where} must be a Literal of strings.")
        elif tp is not str:
            raise ValueError(f"{where} must be a Literal of strings, or str.")
        ask_if = dict(field.metadata.get("ask_if") or {})
        earlier = {question.name for question in questions}
        if not set(ask_if) <= earlier:
            raise ValueError(f"{where}: ask_if must name earlier fields.")
        if ask_if and field.default is dataclasses.MISSING:
            raise ValueError(f"{where} is not always asked, so it needs a default.")
        questions.append(
            Question(field.name, field.metadata.get("prompt"), choices, ask_if)
        )
    return questions


def _check_manual(trigger: Trigger) -> None:
    if trigger.Manual is None:
        return
    if not dataclasses.is_dataclass(trigger.Manual):
        raise ValueError(f"Block {trigger.id}: Manual must be a dataclass.")
    try:
        manual_questions(trigger.Manual)
    except ValueError as e:
        raise ValueError(f"Block {trigger.id}: {e}") from e
