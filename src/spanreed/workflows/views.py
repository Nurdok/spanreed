"""What the web UI shows of a workflow, as JSON data.

The page draws a workflow as a row of cards: the trigger, then the steps.
A card has the block's title, the plugin it comes from, its inputs as they
are stored (templates and all) and the names of its outputs.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from spanreed import config_form
from spanreed.workflows.blocks import Action, Block, BlockKind, Trigger
from spanreed.workflows.engine import WorkflowEngine, outcome
from spanreed.workflows.model import Workflow
from spanreed.workflows.values import to_data

# What the blocks that Spanreed itself publishes are shown as coming from.
CORE_APP = "Spanreed"


def _app(engine: WorkflowEngine, block: Block) -> str:
    plugin = engine.registry.plugin_of(block.id)
    if plugin.canonical_name() == engine.command_plugin:
        return CORE_APP
    return plugin.name()


def _field(name: str, label: str, value: Any) -> dict[str, Any]:
    """Return one input of a card.

    A value that is a mapping, such as the properties to set, also comes as
    ``pairs``: a list keeps its order on the way to the page, and a JSON
    object does not.
    """
    field: dict[str, Any] = {"name": name, "label": label, "value": to_data(value)}
    if isinstance(value, dict):
        field["pairs"] = [[str(key), to_data(item)] for key, item in value.items()]
    return field


def _fields(cls: type[Any], stored: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the values of a block's inputs, in the order of its fields.

    A field that is not stored shows its default. A stored value that the
    block has no field for is shown last, as it is.
    """
    labels = {f["name"]: f["label"] for f in config_form.form_schema(cls)["fields"]}
    shown = []
    for field in dataclasses.fields(cls):
        if field.name in stored:
            value = stored[field.name]
        elif field.default is not dataclasses.MISSING:
            value = field.default
        elif field.default_factory is not dataclasses.MISSING:
            value = field.default_factory()
        else:
            continue
        shown.append(_field(field.name, labels.get(field.name, field.name), value))
    known = {field.name for field in dataclasses.fields(cls)}
    for name, value in stored.items():
        if name not in known:
            shown.append(_field(name, name, value))
    return shown


def _card(
    engine: WorkflowEngine,
    block_id: str,
    stored: dict[str, Any],
    step_id: str,
    expected: type[Block],
) -> dict[str, Any]:
    block = engine.registry.get(block_id)
    if not isinstance(block, expected):
        # The plugin that published the block is gone, or the id is wrong.
        return {
            "id": step_id,
            "block": block_id,
            "title": block_id,
            "app": "",
            "kind": BlockKind.ACTION.value,
            "fields": [_field(name, name, value) for name, value in stored.items()],
            "outputs": [],
            "missing": True,
        }
    if isinstance(block, Trigger):
        inputs, outputs = block.Params, block.Event
    else:
        assert isinstance(block, Action)
        inputs, outputs = block.Input, block.Output
    return {
        "id": step_id,
        "block": block_id,
        "title": block.title,
        "app": _app(engine, block),
        "kind": block.kind.value,
        "fields": _fields(inputs, stored),
        "outputs": [field.name for field in dataclasses.fields(outputs)],
        "missing": False,
    }


def run_view(trace: dict[str, Any]) -> dict[str, Any]:
    """Return the trace of a run, with a sentence end that says how it went."""
    return {**trace, "outcome": outcome(trace)}


def _last_run(trace: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return when and how the last run went, without what its steps held."""
    if trace is None:
        return None
    return {
        "id": trace["id"],
        "started_at": trace["started_at"],
        "status": trace["status"],
        "manual": trace["manual"],
        "outcome": outcome(trace),
    }


def detail(
    engine: WorkflowEngine, workflow: Workflow, last_run: dict[str, Any] | None
) -> dict[str, Any]:
    """Return everything the page shows of one workflow."""
    trigger = _card(
        engine, workflow.trigger.block, workflow.trigger.params, "trigger", Trigger
    )
    steps = [
        {**_card(engine, step.block, step.inputs, step.id, Action), "number": number}
        for number, step in enumerate(workflow.steps, start=1)
    ]
    problems = engine.problems(workflow)
    by_hand = not problems and engine.can_run_by_hand(workflow)
    return {
        "id": workflow.id,
        "name": workflow.name,
        "enabled": workflow.enabled,
        "problems": problems,
        "by_hand": by_hand,
        # The command exists only for a workflow that can be started by
        # hand. ``hidden`` says whether it is kept out of the command menu.
        "command": (
            {"text": workflow.command_text, "hidden": workflow.command_hidden}
            if by_hand
            else None
        ),
        "last_run": _last_run(last_run),
        "trigger": trigger,
        "steps": steps,
    }


def summary(
    engine: WorkflowEngine, workflow: Workflow, last_run: dict[str, Any] | None
) -> dict[str, Any]:
    """Return what the list of workflows shows of one workflow."""
    full = detail(engine, workflow, last_run)
    cards = [full["trigger"], *full["steps"]]
    return {
        "id": full["id"],
        "name": full["name"],
        "enabled": full["enabled"],
        "problem": full["problems"][0] if full["problems"] else None,
        "last_run": full["last_run"],
        # The plugin and the kind of each card, for the row of marks that
        # shows at a glance what the workflow touches.
        "apps": [{"app": card["app"], "kind": card["kind"]} for card in cards],
    }
