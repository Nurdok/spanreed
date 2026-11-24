"""Move a workflow between its stored form and what the editor's forms hold.

The page edits every input as text, because any input can hold a template
(``{{ trigger.date }}``) where its type would ask for a number or a list.
This module says how each input is edited, turns a stored value into the
text the form shows, and turns the text the form sends back into the value
to store.

There are three controls:

- ``text``: one line of text, with templates. A text without a template is
  turned into what the input's type asks for: a number, yes or no, a list.
- ``choice``: one of a fixed set of texts.
- ``rows``: a mapping, edited as rows of a name and a text.

An input of any type (and the value of a row) keeps a text that is a
number, ``true``, ``false``, ``null``, a list or an object in JSON as that
value. Any other text is a text. A text in quotes, ``"5"``, is the text
without the quotes.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from spanreed import config_form
from spanreed.workflows import expressions
from spanreed.workflows.blocks import Action, BlockRegistry, Trigger
from spanreed.workflows.model import Step, TriggerSpec, Workflow

TEXT = "text"
CHOICE = "choice"
ROWS = "rows"

_YES = ("yes", "true", "on")
_NO = ("no", "false", "off")
_HINTS = {
    "integer": "A whole number",
    "number": "A number",
    "boolean": "yes or no",
    "list": "A list",
}


class FormError(ValueError):
    """What the editor sent cannot be stored. ``problems`` says why."""

    def __init__(self, problems: list[str]) -> None:
        """Create the error with a sentence for each problem."""
        super().__init__(" ".join(problems))
        self.problems = problems


def _control(node: dict[str, Any]) -> str:
    if node["kind"] == "choice":
        return CHOICE
    if node["kind"] == "json" and node["mapping"]:
        return ROWS
    return TEXT


def _any_text(value: Any) -> str:
    """Return a value of any type as the text that gives it back."""
    if isinstance(value, str):
        try:
            json.loads(value)
        except ValueError:
            return value
        # The text reads as a number, a list or the like. Quote it, so that
        # it stays a text.
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _any_value(text: str) -> Any:
    """Return what a text stands for, where any type is allowed."""
    if expressions.has_template(text):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _text(node: dict[str, Any], value: Any) -> str:
    """Return a stored value as the text its form control shows."""
    if value is None:
        return ""
    if isinstance(value, str) and node["kind"] != "json":
        # A text, a choice, or a template in place of a typed value.
        return value
    if node["kind"] == "boolean" and isinstance(value, bool):
        return "yes" if value else "no"
    if node["kind"] == "json":
        return _any_text(value)
    return json.dumps(value, ensure_ascii=False)


def _value(node: dict[str, Any], text: str) -> Any:
    """Return the value to store for the text of a control.

    Raise ``ValueError`` if the text is not what the input's type asks for.
    """
    kind = node["kind"]
    if kind == "string":
        return None if text == "" and node.get("optional") else text
    if kind == "json":
        return _any_value(text)
    text = text.strip()
    if expressions.has_template(text):
        # Its value is known only in a run.
        return text
    if text == "":
        if node.get("optional"):
            return None
        raise ValueError("a value is needed.")
    if kind == "choice":
        if text not in node["choices"]:
            raise ValueError(f"“{text}” is not one of the choices.")
        return text
    if kind == "boolean":
        if text.lower() in _YES:
            return True
        if text.lower() in _NO:
            return False
        raise ValueError(f"“{text}” is not yes or no.")
    if kind in ("integer", "number"):
        try:
            return int(text) if kind == "integer" else float(text)
        except ValueError:
            raise ValueError(f"“{text}” is not {_HINTS[kind].lower()}.") from None
    # A list, or a group of fields: written as JSON.
    try:
        value = json.loads(text)
    except ValueError:
        raise ValueError("this must be a value from another step, or JSON.") from None
    if kind == "list" and not isinstance(value, list):
        raise ValueError("this must be a list.")
    return value


def fields(cls: type[Any], stored: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return how each input of a block is edited, by the input's name.

    ``stored`` holds the values the workflow has. An input that is not
    stored shows its default.
    """
    described = {}
    defaults = {field.name: field for field in dataclasses.fields(cls)}
    for node in config_form.form_schema(cls)["fields"]:
        name = node["name"]
        default = defaults[name]
        if name in stored:
            value = stored[name]
        elif default.default is not dataclasses.MISSING:
            value = default.default
        elif default.default_factory is not dataclasses.MISSING:
            value = default.default_factory()
        else:
            value = None
        control = _control(node)
        edit: dict[str, Any] = {
            "control": control,
            "required": bool(node["required"]),
            "hint": node.get("help") or _HINTS.get(node["kind"], ""),
        }
        if control == ROWS:
            rows = value if isinstance(value, dict) else {}
            edit["rows"] = [[str(key), _any_text(item)] for key, item in rows.items()]
        else:
            edit["text"] = _text(node, value)
        if control == CHOICE:
            edit["choices"] = list(node["choices"])
        described[name] = edit
    return described


def _inputs(cls: type[Any], submitted: Any, where: str, found: list[str]) -> dict:
    """Return the inputs to store for what a card's form sent.

    Add a sentence to ``found`` for each value that cannot be stored. An
    input that the form left empty, and that the block does not need, is
    not stored: the block then uses its default.
    """
    if not isinstance(submitted, dict):
        found.append(f"{where}: the inputs are missing.")
        return {}
    inputs: dict[str, Any] = {}
    for node in config_form.form_schema(cls)["fields"]:
        name, label = node["name"], node["label"]
        if name not in submitted:
            continue
        sent = submitted[name]
        if _control(node) == ROWS:
            if not isinstance(sent, list):
                found.append(f"{where}: {label}: rows are expected.")
                continue
            rows: dict[str, Any] = {}
            for row in sent:
                if not (
                    isinstance(row, list)
                    and len(row) == 2
                    and all(isinstance(part, str) for part in row)
                ):
                    found.append(f"{where}: {label}: a row is a name and a text.")
                    continue
                key = row[0].strip()
                if not key:
                    found.append(f"{where}: {label}: a row has no name.")
                elif key in rows:
                    found.append(f"{where}: {label}: “{key}” is there twice.")
                else:
                    rows[key] = _any_value(row[1])
            inputs[name] = rows
            continue
        if not isinstance(sent, str):
            found.append(f"{where}: {label}: a text is expected.")
            continue
        if sent.strip() == "" and not node["required"] and node["kind"] != "string":
            continue
        try:
            inputs[name] = _value(node, sent)
        except ValueError as e:
            found.append(f"{where}: {label}: {e}")
    return inputs


def _steps(
    registry: BlockRegistry,
    submitted: Any,
    prefix: str,
    found: list[str],
    first: int = 1,
) -> list[Step]:
    if not isinstance(submitted, list):
        found.append("The steps are missing.")
        return []
    steps = []
    for number, sent in enumerate(submitted, start=first):
        where = f"Step {prefix}{number}"
        if not isinstance(sent, dict) or not all(
            isinstance(sent.get(key), str) for key in ("id", "block")
        ):
            found.append(f"{where}: a step has an id and a block.")
            continue
        block = registry.get(sent["block"])
        inputs: dict[str, Any] = {}
        if isinstance(block, Action):
            where = f"{where}, {block.title}"
            inputs = _inputs(block.Input, sent.get("inputs", {}), where, found)
        # A block that is not there is a problem of the workflow, which
        # the check of the whole workflow reports.
        inside = _steps(registry, sent.get("steps", []), f"{prefix}{number}.", found)
        steps.append(
            Step(
                id=sent["id"],
                block=sent["block"],
                inputs=inputs,
                steps=inside,
                # The second list of a branch goes on counting.
                otherwise=_steps(
                    registry,
                    sent.get("otherwise", []),
                    f"{prefix}{number}.",
                    found,
                    first=len(inside) + 1,
                ),
            )
        )
    return steps


def workflow_from_form(
    registry: BlockRegistry, workflow: Workflow, form: Any
) -> Workflow:
    """Return a workflow changed to what the editor sent.

    The form holds the name, the text of the command, the trigger with its
    parameters and the steps with their inputs, each input as the text of
    its control. Who owns the workflow, whether it is on and where it came
    from stay as they are.

    Raise ``FormError`` if a value cannot be stored. The result is not
    checked as a whole: ``model.problems`` does that.
    """
    found: list[str] = []
    if not isinstance(form, dict):
        raise FormError(["The workflow is missing."])

    name = form.get("name")
    if not isinstance(name, str) or not name.strip():
        found.append("The workflow needs a name.")
        name = workflow.name
    command_name = form.get("command_name")
    if command_name is not None and not isinstance(command_name, str):
        found.append("The text of the command must be a text.")
        command_name = None

    sent_trigger = form.get("trigger")
    trigger = workflow.trigger
    if not isinstance(sent_trigger, dict) or not isinstance(
        sent_trigger.get("block"), str
    ):
        found.append("The trigger is missing.")
    else:
        block = registry.get(sent_trigger["block"])
        params: dict[str, Any] = {}
        if isinstance(block, Trigger):
            params = _inputs(
                block.Params, sent_trigger.get("params", {}), block.title, found
            )
        trigger = TriggerSpec(block=sent_trigger["block"], params=params)

    steps = _steps(registry, form.get("steps"), "", found)
    if found:
        raise FormError(found)
    return dataclasses.replace(
        workflow,
        name=name.strip(),
        command_name=(command_name or "").strip() or None,
        trigger=trigger,
        steps=steps,
    )
