"""Take a workflow out of Spanreed as a file, and bring one in.

The file is a recipe: the JSON that the recipes in ``recipes/`` are written
in. So a workflow that was exported can be imported by another user, on
another Spanreed, or be put with the recipes that ship.

A file holds the name, the trigger and the steps of a workflow, with every
input as the user wrote it. It does not hold what the workflow remembers,
what its trigger has seen, or its history.

A file that is imported comes from somewhere else, so it is checked from
the ground up, and the workflow starts turned off: its new owner looks at
it before it runs.
"""

from __future__ import annotations

import re
from typing import Any

from spanreed.workflows import model
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.model import Step, TriggerSpec, Workflow, new_workflow_id

# What a file may hold at most. A workflow of this size is no workflow.
MAX_STEPS = 200
MAX_NAME = 100
# The canonical name of the plugin whose blocks every user has.
CORE = "workflows"


class NotAWorkflow(ValueError):
    """A file cannot be imported. ``problems`` says why, for the user."""

    def __init__(self, problems: list[str]) -> None:
        """Create the error with what is wrong with the file."""
        super().__init__("; ".join(problems))
        self.problems = problems


def _blocks(steps: list[Step]) -> set[str]:
    """Return the blocks of some steps and of the steps inside them."""
    found: set[str] = set()
    for step in steps:
        found |= {step.block} | _blocks(step.steps) | _blocks(step.otherwise)
    return found


def slug(name: str) -> str:
    """Return a name as the id of a recipe, and the name of its file."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    return "-".join(words)[:60] or "workflow"


def requires(workflow: Workflow, registry: BlockRegistry) -> list[str]:
    """Return the plugins whose blocks a workflow uses, by canonical name."""
    plugins = set()
    for block in {workflow.trigger.block, *_blocks(workflow.steps)}:
        if registry.get(block) is not None:
            plugins.add(registry.plugin_of(block).canonical_name())
    return sorted(plugins - {CORE})


def exported(workflow: Workflow, registry: BlockRegistry) -> dict[str, Any]:
    """Return a workflow as the data of a recipe."""
    data = workflow.to_data()
    recipe: dict[str, Any] = {
        "id": slug(workflow.name),
        "name": workflow.name,
        "description": "",
        "requires": requires(workflow, registry),
    }
    if workflow.command_name:
        recipe["command_name"] = workflow.command_name
    if workflow.command_hidden:
        recipe["command_hidden"] = True
    recipe["trigger"] = data["trigger"]
    recipe["steps"] = [_step_data(step) for step in data["steps"]]
    return recipe


def _step_data(step: dict[str, Any]) -> dict[str, Any]:
    """Return a step as a recipe holds it: without the lists it does not use."""
    kept = {"id": step["id"], "block": step["block"], "inputs": step["inputs"]}
    for lane in ("steps", "otherwise"):
        if step.get(lane):
            kept[lane] = [_step_data(inside) for inside in step[lane]]
    return kept


def _step(data: Any, count: list[int]) -> Step:
    """Build a step from what a file holds. Raise ``ValueError`` if it is none."""
    if not isinstance(data, dict):
        raise ValueError("A step is not an object.")
    count[0] += 1
    if count[0] > MAX_STEPS:
        raise ValueError(f"It has more than {MAX_STEPS} steps.")
    step_id, block, inputs = data.get("id"), data.get("block"), data.get("inputs", {})
    if not isinstance(step_id, str) or not isinstance(block, str):
        raise ValueError("A step has no id or no block.")
    if not isinstance(inputs, dict):
        raise ValueError(f"The inputs of the step “{step_id}” are not an object.")
    lanes = {}
    for lane in ("steps", "otherwise"):
        inside = data.get(lane, [])
        if not isinstance(inside, list):
            raise ValueError(f"The steps inside “{step_id}” are not a list.")
        lanes[lane] = [_step(item, count) for item in inside]
    return Step(id=step_id, block=block, inputs=inputs, **lanes)


def imported(
    data: Any, user_id: int, registry: BlockRegistry, taken: set[str]
) -> Workflow:
    """Return a new workflow of a user, made from the data of a file.

    The workflow is off. ``taken`` are the names of the user's workflows:
    a workflow is run by its name, so the new one gets a name of its own.

    Raise ``NotAWorkflow`` if the data is no workflow, or one that the
    blocks of this Spanreed cannot run.
    """
    try:
        if not isinstance(data, dict):
            raise ValueError("It does not hold an object.")
        name, trigger = data.get("name"), data.get("trigger")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("It has no name.")
        if not isinstance(trigger, dict) or not isinstance(trigger.get("block"), str):
            raise ValueError("It has no trigger.")
        params = trigger.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("The parameters of its trigger are not an object.")
        steps = data.get("steps")
        if not isinstance(steps, list):
            raise ValueError("It has no list of steps.")
        command_name = data.get("command_name")
        if command_name is not None and not isinstance(command_name, str):
            raise ValueError("The name of its command is not a text.")
        count = [0]
        workflow = Workflow(
            id=new_workflow_id(),
            user_id=user_id,
            name=_free_name(name.strip()[:MAX_NAME], taken),
            trigger=TriggerSpec(block=trigger["block"], params=params),
            steps=[_step(step, count) for step in steps],
            enabled=False,
            command_name=command_name or None,
            command_hidden=data.get("command_hidden") is True,
        )
    except ValueError as e:
        raise NotAWorkflow([f"This is not the file of a workflow. {e}"]) from None
    found = model.problems(workflow, registry)
    if found:
        raise NotAWorkflow(found)
    return workflow


def _free_name(name: str, taken: set[str]) -> str:
    """Return the name, with a number after it if a workflow has it."""
    if name not in taken:
        return name
    number = 2
    while f"{name} ({number})" in taken:
        number += 1
    return f"{name} ({number})"
