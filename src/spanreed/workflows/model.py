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


@dataclass
class Step:
    """One step of a workflow: a block and the inputs to give it.

    Later steps refer to the outputs of this one by its ``id``.
    """

    id: str
    block: str
    inputs: dict[str, Any] = field(default_factory=dict)


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
        values["steps"] = [Step(**step) for step in data["steps"]]
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

    known_names = {TRIGGER_NAME, RUN_NAME}
    for number, step in enumerate(workflow.steps, start=1):
        where = f"Step {number}"
        if not _STEP_ID_RE.match(step.id):
            found.append(
                f"{where}: the id “{step.id}” must be lower-case letters, "
                "digits and “_”, and start with a letter."
            )
        elif step.id in known_names:
            found.append(f"{where}: the id “{step.id}” is already in use.")

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
