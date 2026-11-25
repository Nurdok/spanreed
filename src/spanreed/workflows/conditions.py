"""Conditions as the editor shows them: rows of a value and what is asked of it.

A condition is stored as one template that comes to yes or no::

    "{{ run.manual and find.count > 0 }}"

The engine fills it in as any other template, and nothing about that
changes here. This module moves between that text and what the editor
shows in its place, so that nobody has to write the template:

- **rows**, each a value, what is asked of it ("is bigger than"), and for
  some questions a second value;
- whether **all** of the rows have to hold, or **any** of them.

A value is written as in any text box of the editor: text, with the values
of earlier steps as ``{{ step.name }}``.

Not every template is a list of rows: ``{{ a and (b or c) }}`` is not. Such
a condition stays as it is written, and the editor shows its text. A
template is only ever shown as rows if the rows give back a template that
means the same (the same syntax tree), so opening a workflow and saving it
never changes what a condition asks.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import jinja2

from spanreed.config_form import HELP_KEY
from spanreed.workflows import expressions

# The key of the field metadata that says: this input is a condition.
CONDITION_KEY = "workflow_condition"

ALL = "all"
ANY = "any"


def condition(text: str = "") -> Any:
    """Return the field of an input that is a condition.

    The editor shows it as rows. ``text`` is the help of the input.
    """
    metadata: dict[str, Any] = {CONDITION_KEY: True}
    if text:
        metadata[HELP_KEY] = text
    return dataclasses.field(metadata=metadata)


def is_condition(field: dataclasses.Field[Any]) -> bool:
    """Return whether the field of an input is a condition."""
    return bool(field.metadata.get(CONDITION_KEY))


class ConditionError(ValueError):
    """Rows cannot be made into a condition. The message is for the user."""


@dataclasses.dataclass(frozen=True)
class Question:
    """What can be asked of a value."""

    id: str
    # As the editor says it, between the two values.
    label: str
    # Whether it takes a second value.
    second: bool
    # The kinds of value it is for: the editor offers it for a value of
    # one of them, and for a value whose kind it does not know.
    kinds: tuple[str, ...]
    # The expression, with ``{left}`` and ``{right}``.
    expression: str


YES_NO = "yes or no"
NUMBER = "number"
TEXT = "text"
LIST = "list"

QUESTIONS = [
    Question("true", "is true", False, (YES_NO,), "{left}"),
    Question("false", "is false", False, (YES_NO,), "not {left}"),
    Question("is", "is", True, (NUMBER, TEXT), "{left} == {right}"),
    Question("is-not", "is not", True, (NUMBER, TEXT), "{left} != {right}"),
    Question("bigger", "is bigger than", True, (NUMBER,), "{left} > {right}"),
    Question("at-least", "is at least", True, (NUMBER,), "{left} >= {right}"),
    Question("smaller", "is smaller than", True, (NUMBER,), "{left} < {right}"),
    Question("at-most", "is at most", True, (NUMBER,), "{left} <= {right}"),
    Question(
        "contains", "contains", True, (TEXT, LIST), "{left} is containing({right})"
    ),
    Question(
        "lacks",
        "does not contain",
        True,
        (TEXT, LIST),
        "{left} is not containing({right})",
    ),
    Question("empty", "is empty", False, (TEXT, LIST), "{left} is empty"),
    Question("not-empty", "is not empty", False, (TEXT, LIST), "{left} is not empty"),
]
_BY_ID = {question.id: question for question in QUESTIONS}


def questions() -> list[dict[str, Any]]:
    """Return what can be asked of a value, for the page."""
    return [
        {
            "id": question.id,
            "label": question.label,
            "second": question.second,
            "kinds": list(question.kinds),
        }
        for question in QUESTIONS
    ]


def empty_rows() -> dict[str, Any]:
    """Return the rows of a condition that nobody has written yet."""
    return {"join": ALL, "rows": [["", "true", ""]]}


# -- from a value in a text box to an expression, and back ---------------------

_ONE_TEMPLATE_RE = re.compile(
    r"\s*\{\{(?P<inside>(?:(?!\{\{|\}\}|\{%|%\}).)+)\}\}\s*", re.S
)
_TEMPLATE_RE = re.compile(r"\{\{((?:(?!\{\{|\}\}).)+)\}\}", re.S)
_NUMBER_RE = re.compile(r"-?\d+(\.\d+)?")
# A name, and what is read from it: ``find.tasks[0].name``. It needs no
# parentheses, wherever it stands.
_ATOM_RE = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*|\[[^\[\]()]*\])*")
_YES = ("yes", "true")
_NO = ("no", "false")


def _atom(expression: str) -> str:
    """Return an expression that can stand next to anything."""
    expression = expression.strip()
    if _ATOM_RE.fullmatch(expression) or _literal_text(expression) is not None:
        return expression
    return f"({expression})"


def _expression(value: str, what: str) -> str:
    """Return the expression for a value as it is written in a text box."""
    text = value.strip()
    if not text:
        raise ConditionError(f"{what} is missing.")
    if "{%" in text:
        raise ConditionError(f"{what} cannot hold “{{% … %}}”.")
    one = _ONE_TEMPLATE_RE.fullmatch(text)
    if one is not None:
        return _atom(one["inside"])
    if "{{" not in text:
        if _NUMBER_RE.fullmatch(text):
            return text
        if text.lower() in _YES:
            return "true"
        if text.lower() in _NO:
            return "false"
        if len(text) >= 2 and text[0] == text[-1] == '"':
            # In quotes: the text between them, also if it reads as a number.
            text = text[1:-1]
        return json.dumps(text, ensure_ascii=False)
    # Text with values in it: put together from its parts.
    parts, at = [], 0
    for found in _TEMPLATE_RE.finditer(value):
        if found.start() > at:
            parts.append(json.dumps(value[at : found.start()], ensure_ascii=False))
        parts.append(_atom(found.group(1)))
        at = found.end()
    if at < len(value):
        parts.append(json.dumps(value[at:], ensure_ascii=False))
    return "(" + " ~ ".join(parts) + ")"


def _literal_text(expression: str) -> str | None:
    """Return how a literal is written in a text box, or ``None`` for no literal."""
    if _NUMBER_RE.fullmatch(expression):
        return expression
    if expression == "true":
        return "yes"
    if expression == "false":
        return "no"
    if (
        len(expression) >= 2
        and expression[0] == expression[-1]
        and expression[0] in "'\""
    ):
        try:
            parsed = _env().parse("{{ " + expression + " }}")
        except jinja2.TemplateError:
            return None
        output = parsed.body[0] if parsed.body else None
        nodes = getattr(output, "nodes", [])
        if len(nodes) != 1 or not isinstance(nodes[0], jinja2.nodes.Const):
            return None
        text = nodes[0].value
        if not isinstance(text, str) or "{{" in text or "{%" in text:
            return None
        plain = text.strip() == text and text != ""
        reads_as_other = bool(_NUMBER_RE.fullmatch(text)) or text.lower() in _YES + _NO
        quoted = len(text) >= 2 and text[0] == text[-1] == '"'
        if plain and not reads_as_other and not quoted:
            return text
        # What would be read as something else is kept in quotes.
        return f'"{text}"'
    return None


def _value(expression: str) -> str:
    """Return an expression as the value a text box shows."""
    expression = _unwrapped(expression.strip())
    literal = _literal_text(expression)
    if literal is not None:
        return literal
    return "{{ " + expression + " }}"


# -- reading a template as rows --------------------------------------------------


def _mask(text: str) -> str:
    """Return a text with what is in quotes or in brackets blanked out.

    Each character keeps its place, so what is found in the mask can be cut
    out of the text. Raise ``ValueError`` if a quote or a bracket is open
    at the end.
    """
    masked, depth, quote, skip = [], 0, "", False
    for char in text:
        if quote:
            masked.append("_")
            if skip:
                skip = False
            elif char == "\\":
                skip = True
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            masked.append("_")
        elif char in "([{":
            depth += 1
            masked.append("_")
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                raise ValueError("a bracket is closed that was not opened")
            masked.append("_")
        else:
            masked.append("_" if depth else char)
    if quote or depth:
        raise ValueError("a quote or a bracket is not closed")
    return "".join(masked)


def _unwrapped(expression: str) -> str:
    """Return an expression without parentheses that hold all of it."""
    while expression.startswith("(") and expression.endswith(")"):
        try:
            # "(a) and (b)" starts and ends with one too: what is between
            # the first and the last is then no expression of its own.
            _mask(expression[1:-1])
        except ValueError:
            break
        expression = expression[1:-1].strip()
    return expression


_COMPARISONS = [
    ("is-not", re.compile(r"!=")),
    ("at-least", re.compile(r">=")),
    ("at-most", re.compile(r"<=")),
    ("is", re.compile(r"(?<![=!<>])==")),
    ("bigger", re.compile(r">(?!=)")),
    ("smaller", re.compile(r"<(?!=)")),
]
_ANY_COMPARISON_RE = re.compile(r"==|!=|>=|<=|>|<|\bnot\s+in\b|\bin\b")
_EMPTY_RE = re.compile(r"\s+is\s+(not\s+)?empty\s*$")
# In the mask, what is in the parentheses is blanked out, and so are they.
_CONTAINING_RE = re.compile(r"\s+is\s+(not\s+)?containing(_+)\s*$")
_NOT_RE = re.compile(r"^\s*not\s+")


def _row(part: str) -> list[str]:
    """Return one side of an "and" or an "or" as a row.

    What is no question that the editor knows is the value of "is true":
    a row can hold any expression that comes to yes or no.
    """
    part = _unwrapped(part.strip())
    mask = _mask(part)
    negated = _NOT_RE.match(mask)
    if negated is not None:
        return [_value(part[negated.end() :]), "false", ""]
    empty = _EMPTY_RE.search(mask)
    if empty is not None:
        question = "not-empty" if empty.group(1) else "empty"
        return [_value(part[: empty.start()]), question, ""]
    holds = _CONTAINING_RE.search(mask)
    if holds is not None:
        inside = part[holds.start(2) + 1 : holds.end(2) - 1]
        question = "lacks" if holds.group(1) else "contains"
        return [_value(part[: holds.start()]), question, _value(inside)]
    if len(_ANY_COMPARISON_RE.findall(mask)) == 1:
        for question, pattern in _COMPARISONS:
            found = pattern.search(mask)
            if found is not None:
                return [
                    _value(part[: found.start()]),
                    question,
                    _value(part[found.end() :]),
                ]
    return [_value(part), "true", ""]


_JOIN_RE = re.compile(r"\s+(and|or)\s+")


def from_template(text: Any) -> dict[str, Any] | None:
    """Return a stored condition as rows, or ``None`` if it is not rows.

    ``{"join": "all" or "any", "rows": [[value, question, second value]]}``.
    A condition that nobody has written yet is one empty row.
    """
    if text is None or text == "":
        return empty_rows()
    if not isinstance(text, str):
        return None
    one = _ONE_TEMPLATE_RE.fullmatch(text)
    if one is None:
        return None
    inside = one["inside"].strip()
    try:
        mask = _mask(inside)
        joins = {found.group(1) for found in _JOIN_RE.finditer(mask)}
        if len(joins) > 1:
            # "a and b or c": which goes with which is not a list of rows.
            return None
        parts, at = [], 0
        for found in _JOIN_RE.finditer(mask):
            parts.append(inside[at : found.start()])
            at = found.end()
        parts.append(inside[at:])
        shown = {
            "join": ANY if joins == {"or"} else ALL,
            "rows": [_row(part) for part in parts],
        }
        again = to_template(shown)
    except (ValueError, jinja2.TemplateError):
        return None
    return shown if _same(text, again) else None


def _env() -> jinja2.Environment:
    return expressions._env


def _same(one: str, other: str) -> bool:
    """Return whether two templates mean the same: the same syntax tree."""
    try:
        return bool(_env().parse(one) == _env().parse(other))
    except jinja2.TemplateError:
        return False


# -- making a template from rows -------------------------------------------------


def to_template(shown: Any) -> str:
    """Return the template that asks what some rows ask.

    Raise ``ConditionError`` if they are no rows, or if a row lacks a value.
    """
    if not isinstance(shown, dict) or not isinstance(shown.get("rows"), list):
        raise ConditionError("rows are expected.")
    join, rows = shown.get("join", ALL), shown["rows"]
    if join not in (ALL, ANY):
        raise ConditionError("the rows are joined by “all” or by “any”.")
    if not rows:
        raise ConditionError("a condition has at least one row.")
    parts = []
    for number, row in enumerate(rows, start=1):
        if not (
            isinstance(row, list)
            and len(row) == 3
            and all(isinstance(x, str) for x in row)
        ):
            raise ConditionError("a row is a value, a question and a second value.")
        left, question_id, right = row
        question = _BY_ID.get(question_id)
        if question is None:
            raise ConditionError(f"“{question_id}” is nothing that can be asked.")
        where = f"row {number}: " if len(rows) > 1 else ""
        values = {"left": _expression(left, f"{where}the value")}
        if question.second:
            values["right"] = _expression(
                right, f"{where}the value after “{question.label}”"
            )
        parts.append(question.expression.format(**values))
    text = "{{ " + (" and " if join == ALL else " or ").join(parts) + " }}"
    try:
        _env().parse(text)
    except jinja2.TemplateError as e:
        raise ConditionError(f"this is no condition that can be worked out: {e}") from e
    return text
