"""A workflow as data: what is stored, and the check that it makes sense."""

from __future__ import annotations

import dataclasses
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from spanreed.workflows import expressions
from spanreed.workflows.blocks import Action, BlockRegistry, Trigger
from spanreed.workflows.values import from_data

# Names that step inputs can refer to besides the ids of earlier steps.
TRIGGER_NAME = "trigger"
RUN_NAME = "run"
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Words that a template reads as something else than the name of a step.
RESERVED_IDS = frozenset(
    {"and", "or", "not", "in", "is", "if", "else", "true", "false", "none"}
)


@dataclass
class Step:
    """One step of a workflow: a block and the inputs to give it.

    Later steps refer to the outputs of this one by its ``id``.
    """

    id: str
    block: str
    inputs: dict[str, Any] = field(default_factory=dict)
    # The steps inside this one, if its block holds steps. A loop runs them
    # once for each round, and a branch runs them if it goes the first way.
    steps: list[Step] = field(default_factory=list)
    # The steps that a branch runs if it goes the other way.
    otherwise: list[Step] = field(default_factory=list)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Step:
        """Build a step, and the steps inside it, from JSON data."""
        values = dict(data)
        for lane in ("steps", "otherwise"):
            values[lane] = [cls.from_data(step) for step in data.get(lane, [])]
        return cls(**values)


@dataclass
class TriggerSpec:
    """The trigger of a workflow: a block and its parameters."""

    block: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Workflow:
    """A workflow of one user."""

    id: str
    user_id: int
    name: str
    trigger: TriggerSpec
    steps: list[Step]
    # A workflow that is off does not run by itself and has no command.
    enabled: bool = True
    # The text of the workflow's command. ``None`` means the name.
    command_name: str | None = None
    # A workflow whose trigger can be started by hand is a command in the
    # command menu, unless this hides it.
    command_hidden: bool = False
    # The id of the recipe this workflow was installed from, if any.
    recipe: str | None = None

    @property
    def command_text(self) -> str:
        """Return the text of the workflow's command."""
        return self.command_name or self.name

    def to_data(self) -> dict[str, Any]:
        """Return the workflow as JSON data."""
        return dataclasses.asdict(self)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Workflow:
        """Build a workflow from the JSON data that ``to_data`` gives."""
        known = {f.name for f in dataclasses.fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["trigger"] = TriggerSpec(**data["trigger"])
        values["steps"] = [Step.from_data(step) for step in data["steps"]]
        return cls(**values)


def new_workflow_id() -> str:
    """Return an id for a new workflow."""
    return f"wf_{secrets.token_hex(6)}"


def problems(workflow: Workflow, registry: BlockRegistry) -> list[str]:
    """Return what is wrong with a workflow, as sentences for the user.

    An empty list means that the engine can run the workflow. Check that
    every block exists, that the inputs fit the blocks, and that every
    template refers to the trigger, the run or an earlier step.
    """
    found: list[str] = []

    trigger = registry.get(workflow.trigger.block)
    if not isinstance(trigger, Trigger):
        found.append(f"There is no trigger “{workflow.trigger.block}”.")
    else:
        try:
            from_data(trigger.Params, workflow.trigger.params)
        except ValueError as e:
            found.append(f"{trigger.title}: {e}")

    found.extend(
        _step_problems(
            workflow.steps,
            registry,
            known_names={TRIGGER_NAME, RUN_NAME},
            used_ids={TRIGGER_NAME, RUN_NAME},
            prefix="",
        )
    )
    return found


def _step_problems(
    steps: list[Step],
    registry: BlockRegistry,
    known_names: set[str],
    used_ids: set[str],
    prefix: str,
    first: int = 1,
) -> list[str]:
    """Return what is wrong with some steps and the steps inside them.

    ``known_names`` holds what a template of the first step can refer to.
    ``used_ids`` holds every id of the workflow so far, also of steps that
    cannot be referred to from here. Both are added to. ``first`` is the
    number of the first step: the second list of a branch goes on counting
    where the first stopped.
    """
    found: list[str] = []
    for number, step in enumerate(steps, start=first):
        label = f"{prefix}{number}"
        where = f"Step {label}"
        if not _STEP_ID_RE.match(step.id):
            found.append(
                f"{where}: the id “{step.id}” must be lower-case letters, "
                "digits and “_”, and start with a letter."
            )
        elif step.id in used_ids:
            found.append(f"{where}: the id “{step.id}” is already in use.")
        elif step.id in RESERVED_IDS:
            found.append(
                f"{where}: the id “{step.id}” is a word that templates read as "
                "something else."
            )
        used_ids.add(step.id)

        block = registry.get(step.block)
        if not isinstance(block, Action):
            found.append(f"{where}: there is no block “{step.block}”.")
        else:
            where = f"{where}, {block.title}"
            found.extend(f"{where}: {p}" for p in _input_problems(block, step))

        try:
            unknown = expressions.referenced_names(step.inputs) - known_names
        except expressions.ExpressionError as e:
            found.append(f"{where}: {e}")
        else:
            for name in sorted(unknown):
                found.append(
                    f"{where}: “{name}” is not the trigger, the run or an "
                    "earlier step."
                )
        known_names.add(step.id)

        lanes = [key for key, _ in block.lanes()] if isinstance(block, Action) else []
        counted = 1
        for key, inside in (("steps", step.steps), ("otherwise", step.otherwise)):
            if key in lanes:
                # The steps inside can refer to what is known here and to
                # each other. The steps after cannot refer to them: what
                # they give differs from round to round, or they did not
                # run at all.
                found.extend(
                    _step_problems(
                        inside,
                        registry,
                        known_names=set(known_names),
                        used_ids=used_ids,
                        prefix=f"{label}.",
                        first=counted,
                    )
                )
                counted += len(inside)
            elif inside and key == "steps":
                found.append(
                    f"{where}: only a block that repeats steps can hold steps."
                )
            elif inside:
                found.append(
                    f"{where}: only a block that chooses between steps can "
                    "hold “otherwise” steps."
                )
    return found


def _input_problems(block: Action, step: Step) -> list[str]:
    fields = {f.name: f for f in dataclasses.fields(block.Input)}
    found = [
        f"there is no input “{name}”." for name in step.inputs if name not in fields
    ]
    if found:
        return found
    if expressions.has_template(step.inputs):
        # The values are known only in a run. Check what can be checked now:
        # that every input without a default is given.
        return [
            f"the input “{name}” is missing."
            for name, f in fields.items()
            if name not in step.inputs
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ]
    try:
        from_data(block.Input, step.inputs)
    except ValueError as e:
        return [str(e)]
    return []
