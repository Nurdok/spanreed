"""The blocks that workflows are made of, and the registry that holds them.

A block has an id, a title, and typed inputs and outputs. The inputs and
outputs are dataclasses, so a form and a check of the values come from the
type hints, as for a plugin config (see ``spanreed.config_form``).

There are two shapes of block:

- An ``Action`` is a step. It gets its inputs and returns its outputs.
  Blocks that ask the user something and blocks that hold logic are actions
  too; ``kind`` says which, for the editor.
- A ``Trigger`` starts runs. It yields one event for each run, and says
  whether, and how, it can be started by hand.

A plugin publishes its blocks from ``Plugin.blocks()``.
"""

from __future__ import annotations

import abc
import dataclasses
import enum
import re
import typing
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Protocol, TypeVar

from spanreed.user import User

if TYPE_CHECKING:
    from spanreed.plugin import Plugin

InT = TypeVar("InT")
OutT = TypeVar("OutT")
ParamsT = TypeVar("ParamsT")
EventT = TypeVar("EventT")

_BLOCK_ID_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")


class BlockKind(enum.StrEnum):
    """What a block is for. The editor groups and colors blocks by it."""

    TRIGGER = "trigger"
    ACTION = "action"
    ASK = "ask"
    LOGIC = "logic"


class StopRun(Exception):
    """Raised by a block to end the run at this step without failing it."""

    def __init__(self, reason: str) -> None:
        """Create the signal. ``reason`` is shown in the run's trace."""
        super().__init__(reason)
        self.reason = reason


@dataclass
class NoOutput:
    """The outputs of a block that has none."""


@dataclass
class NoQuestion:
    """The question of a trigger that a manual run starts without asking."""


@dataclass(frozen=True)
class RunInfo:
    """What a step can know about the run it is part of.

    Step inputs can refer to it as ``run``: ``{{ run.manual }}``.
    """

    id: str
    # True if the user started the run by hand.
    manual: bool


@dataclass(frozen=True)
class StepContext:
    """What the engine hands to an action besides its inputs."""

    user: User
    workflow_id: str
    run: RunInfo


class TriggerState(Protocol):
    """Small values a trigger keeps between events, such as a cursor.

    They are kept for each workflow and survive a restart.
    """

    async def get(self, name: str) -> str | None:
        """Return a stored value, or ``None`` if it is not set."""

    async def set(self, name: str, value: str) -> None:
        """Store a value."""


@dataclass(frozen=True)
class TriggerContext:
    """What the engine hands to a trigger besides its parameters."""

    user: User
    workflow_id: str
    state: TriggerState


def _type_arguments(cls: type, generic: type) -> tuple[Any, ...] | None:
    """Return the types a class gave to its generic base, if it gave any."""
    for base in getattr(cls, "__orig_bases__", ()):
        if typing.get_origin(base) is generic:
            return typing.get_args(base)
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
    """

    kind = BlockKind.ACTION
    Input: ClassVar[type[Any]]
    Output: ClassVar[type[Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Take ``Input`` and ``Output`` from the type arguments."""
        super().__init_subclass__(**kwargs)
        arguments = _type_arguments(cls, Action)
        if arguments is not None:
            cls.Input, cls.Output = arguments

    @abc.abstractmethod
    async def run(self, ctx: StepContext, inputs: InT) -> OutT:
        """Do the step for one run."""


class Trigger(Block, Generic[ParamsT, EventT]):
    """What starts a run. Each event that ``events`` yields is one run.

    Name the parameters and the event as the type arguments. The fields of
    the event are the trigger's outputs: steps refer to them as
    ``{{ trigger.<field> }}``.

    ``Manual`` says whether the user can start a run by hand. ``None`` means
    no. Otherwise it is a dataclass that holds what to ask first: each field
    is a ``Literal`` of the choices, and ``NoQuestion`` asks nothing. A
    trigger with a ``Manual`` implements ``manual_events``.
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
    def events(self, ctx: TriggerContext, params: ParamsT) -> AsyncIterator[EventT]:
        """Yield an event each time the workflow should run.

        The engine runs the workflow for an event before it asks for the
        next one, so code after a ``yield`` runs when that run is over. The
        engine cancels the iterator when the workflow is turned off, and
        starts it again, after a pause, if it raises.
        """

    async def manual_events(
        self, ctx: TriggerContext, params: ParamsT, answer: Any
    ) -> list[EventT]:
        """Return the events of a run started by hand.

        ``answer`` is an instance of ``Manual``, filled in by the user.
        """
        raise NotImplementedError(f"{self.id} cannot be started by hand.")


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


def manual_choices(manual: type[Any]) -> list[tuple[str, list[str]]]:
    """Return the questions of a ``Manual``: each field with its choices.

    Raise ``ValueError`` if a field is not a ``Literal`` of strings.
    """
    hints = typing.get_type_hints(manual)
    questions = []
    for field in dataclasses.fields(manual):
        tp = hints[field.name]
        choices = list(typing.get_args(tp))
        if typing.get_origin(tp) is not typing.Literal or not all(
            isinstance(choice, str) for choice in choices
        ):
            raise ValueError(
                f"{manual.__qualname__}.{field.name} must be a Literal of strings."
            )
        questions.append((field.name, choices))
    return questions


def _check_manual(trigger: Trigger) -> None:
    if trigger.Manual is None:
        return
    if not dataclasses.is_dataclass(trigger.Manual):
        raise ValueError(f"Block {trigger.id}: Manual must be a dataclass.")
    try:
        manual_choices(trigger.Manual)
    except ValueError as e:
        raise ValueError(f"Block {trigger.id}: {e}") from e
