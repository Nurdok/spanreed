"""What the web UI shows of a workflow, as JSON data.

The page draws a workflow as a row of cards: the trigger, then the steps.
A card has the block's title, the plugin it comes from, its inputs as they
are stored (templates and all) and the names of its outputs. The card of a
loop also has the cards of the steps inside it.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Iterable
from typing import Any

from spanreed import config_form
from spanreed.workflows import editing
from spanreed.workflows.blocks import (
    Action,
    Block,
    BlockKind,
    RunInfo,
    Trigger,
)
from spanreed.workflows.engine import WorkflowEngine, outcome
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.values import to_data

# What the blocks that Spanreed itself publishes are shown as coming from.
CORE_APP = "Spanreed"


# How many of the names in a workflow's memory the page gets.
MEMORY_SHOWN = 200


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

    A field that is not stored shows its default, or nothing if it has
    none: the editor has to show an input that still needs a value. A
    stored value that the block has no field for is shown last, as it is.
    Each field of the block also says how the editor edits it (see
    ``spanreed.workflows.editing``).
    """
    labels = {f["name"]: f["label"] for f in config_form.form_schema(cls)["fields"]}
    edits = editing.fields(cls, stored)
    shown = []
    for field in dataclasses.fields(cls):
        if field.name in stored:
            value = stored[field.name]
        elif field.default is not dataclasses.MISSING:
            value = field.default
        elif field.default_factory is not dataclasses.MISSING:
            value = field.default_factory()
        else:
            value = None
        shown.append(_field(field.name, labels.get(field.name, field.name), value))
        if field.name in edits:
            shown[-1]["edit"] = edits[field.name]
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
        "item_outputs": _item_outputs(outputs),
        "missing": False,
    }


def _item_outputs(outputs: type[Any]) -> dict[str, list[str]]:
    """Return the fields of the items of each output that is a list of them.

    The editor offers them inside a loop over such an output: a loop over
    a workout's ``sets`` can use ``item.reps``.
    """
    described = {}
    hints = typing.get_type_hints(outputs)
    for field in dataclasses.fields(outputs):
        tp = hints[field.name]
        items = typing.get_args(tp)
        if (
            typing.get_origin(tp) is list
            and len(items) == 1
            and isinstance(items[0], type)
            and dataclasses.is_dataclass(items[0])
        ):
            described[field.name] = [f.name for f in dataclasses.fields(items[0])]
    return described


def _lanes(block: Block | None) -> list[dict[str, str]]:
    """Return the lists of steps that a step of a block holds, with their titles."""
    if not isinstance(block, Action):
        return []
    return [{"key": key, "title": title} for key, title in block.lanes()]


def _step_cards(
    engine: WorkflowEngine, steps: list[Step], prefix: str = "", first: int = 1
) -> list[dict[str, Any]]:
    """Return the cards of some steps, each with the cards of its own steps.

    ``number`` is what the page and the engine's messages call a step:
    ``2``, and ``2.1`` for the first step inside it. The second list of a
    branch goes on counting where the first stopped.
    """
    cards = []
    for position, step in enumerate(steps, start=first):
        number = f"{prefix}{position}"
        card = _card(engine, step.block, step.inputs, step.id, Action)
        card["number"] = number
        card["steps"] = _step_cards(engine, step.steps, prefix=f"{number}.")
        card["otherwise"] = _step_cards(
            engine, step.otherwise, prefix=f"{number}.", first=len(step.steps) + 1
        )
        card["lanes"] = _lanes(engine.registry.get(step.block))
        cards.append(card)
    return cards


def _flat(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return some cards and the cards inside them, in the order they run."""
    flat = []
    for card in cards:
        flat.append(card)
        flat.extend(_flat(card.get("steps", [])))
        flat.extend(_flat(card.get("otherwise", [])))
    return flat


def _description(block: Block) -> str:
    """Return what a block does, in a sentence: the start of its docstring."""
    doc = inspect.getdoc(type(block)) or ""
    return " ".join(doc.split("\n\n")[0].split())


def block_entry(
    engine: WorkflowEngine, block: Block, connected: bool
) -> dict[str, Any]:
    """Return what the editor's list of blocks shows of one block.

    ``connected`` says whether the user is registered to the block's
    plugin. ``card`` is the card of a new step of the block, with every
    input at its default.
    """
    expected: type[Block] = Trigger if isinstance(block, Trigger) else Action
    card = _card(engine, block.id, {}, "", expected)
    card["steps"] = []
    card["otherwise"] = []
    card["lanes"] = _lanes(block)
    return {
        "block": block.id,
        "title": block.title,
        "app": card["app"],
        "kind": block.kind.value,
        "description": _description(block),
        "connected": connected,
        # A trigger that cannot be started by hand gives no command.
        "by_hand": isinstance(block, Trigger) and block.Manual is not None,
        "card": card,
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


def _writes(engine: WorkflowEngine, steps: list[Step]) -> set[str]:
    """Return the names of what some steps, and the steps inside them, write."""
    written: set[str] = set()
    for step in steps:
        block = engine.registry.get(step.block)
        if isinstance(block, Action):
            written.update(block.writes(step.inputs))
        written |= _writes(engine, step.steps) | _writes(engine, step.otherwise)
    return written


def warnings(
    engine: WorkflowEngine, workflow: Workflow, others: Iterable[Workflow]
) -> list[str]:
    """Return what the user should know about a workflow that can run.

    Two workflows may write the same thing, a property of the daily note
    for example. That is allowed, and one of them wins. Say which other
    workflows that are on write what this one writes.
    """
    mine = _writes(engine, workflow.steps)
    found = []
    for other in others:
        if other.id == workflow.id or not other.enabled:
            continue
        shared = sorted(mine & _writes(engine, other.steps))
        if shared:
            found.append(f"“{other.name}” also writes {', '.join(shared)}.")
    return found


def _event_summary(event: Any) -> str:
    """Return the start of an event in a line: its plain values by name."""
    if not isinstance(event, dict):
        return ""
    parts = [
        f"{name} {value}"
        for name, value in event.items()
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
    ]
    text = " · ".join(parts)
    return text if len(text) <= 120 else text[:119] + "…"


def detail(
    engine: WorkflowEngine,
    workflow: Workflow,
    last_run: dict[str, Any] | None,
    others: Iterable[Workflow] = (),
    test_events: dict[str, Any] | None = None,
    remembers: Iterable[str] = (),
) -> dict[str, Any]:
    """Return everything the page shows of one workflow.

    ``others`` are the user's other workflows, for what they write too.
    ``test_events`` is what the workflow can be tried out with (see
    ``WorkflowEngine.test_events``). ``remembers`` are the names its memory
    keeps something under.
    """
    names = list(remembers)
    trigger = _card(
        engine, workflow.trigger.block, workflow.trigger.params, "trigger", Trigger
    )
    steps = _step_cards(engine, workflow.steps)
    problems = engine.problems(workflow)
    by_hand = not problems and engine.can_run_by_hand(workflow)
    return {
        "id": workflow.id,
        "name": workflow.name,
        "enabled": workflow.enabled,
        "problems": problems,
        "warnings": warnings(engine, workflow, others),
        # What the editor's Test can run for. The last event is named by
        # when it was and how it starts: the event itself can be large.
        "test": {
            "last": (
                {
                    "at": test_events["last"]["at"],
                    "summary": _event_summary(test_events["last"]["event"]),
                }
                if test_events and test_events["last"]
                else None
            ),
            "now": bool(test_events and test_events["now"]),
            # A workflow that asks the user something is not tested.
            "asks": engine.asks(workflow),
        },
        "by_hand": by_hand,
        # The command exists only for a workflow that can be started by
        # hand. ``hidden`` says whether it is kept out of the command menu.
        "command": (
            {"text": workflow.command_text, "hidden": workflow.command_hidden}
            if by_hand
            else None
        ),
        # The text the command was given. ``None``: it is the name.
        "command_name": workflow.command_name,
        "last_run": _last_run(last_run),
        # What the workflow remembers between runs: how many names, and
        # the first of them.
        "remembers": {"count": len(names), "names": names[:MEMORY_SHOWN]},
        # What ``{{ run.… }}`` offers.
        "run_outputs": [field.name for field in dataclasses.fields(RunInfo)],
        "trigger": trigger,
        "steps": steps,
    }


def summary(
    engine: WorkflowEngine, workflow: Workflow, last_run: dict[str, Any] | None
) -> dict[str, Any]:
    """Return what the list of workflows shows of one workflow."""
    full = detail(engine, workflow, last_run)
    cards = [full["trigger"], *_flat(full["steps"])]
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
