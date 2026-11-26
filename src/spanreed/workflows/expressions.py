"""Fill step inputs with the outputs of the trigger and of earlier steps.

A step input is JSON data. A text in it can refer to earlier outputs in
Jinja syntax, the syntax that the note templates of Lit Notes and Recurring
Payments already use::

    "Updated {{ find.count }} overdue tasks to today."

A text that is one expression and nothing else gives the value itself, with
its type, so that a list or a number can go from one step to the next::

    "{{ find.tasks }}"        a list
    "{{ find.count > 0 }}"    yes or no

Templates run in Jinja's sandbox, and a name that does not exist is an
error, not an empty text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import jinja2
import jinja2.meta
import jinja2.sandbox

_env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)


def _is_empty(value: Any) -> bool:
    """Return whether a value holds nothing: no text, no items, or nothing.

    ``0`` and "no" are values: they are not empty.
    """
    if value is None or isinstance(value, jinja2.Undefined):
        return True
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return len(value) == 0
    return False


# ``{{ find.tasks is empty }}`` and ``{{ note.value is not empty }}``.
_env.tests["empty"] = _is_empty


def _is_containing(value: Any, part: Any) -> bool:
    """Return whether a text has a text in it, or a list an item.

    In a text, what is looked for is read as text: "2026" is in a date,
    also where it was written as a number.
    """
    if isinstance(value, str):
        return str(part) in value
    try:
        return part in value
    except TypeError:
        return False


# ``{{ note.tags is containing("book") }}``.
_env.tests["containing"] = _is_containing

# A text that is exactly one ``{{ ... }}``.
_WHOLE_EXPRESSION_RE = re.compile(
    r"^\s*\{\{(?P<expression>(?:(?!\{\{|\}\}|\{%|%\}).)+)\}\}\s*$", re.DOTALL
)


# How the message ends when a template comes to no value at all.
NO_SUCH_VALUE = "there is no such value."


class _Nothing(jinja2.ChainableUndefined):
    """Nothing, whatever is read of it. What is asked of it does not hold."""

    def _no(self, other: Any) -> bool:
        return False

    __lt__ = __le__ = __gt__ = __ge__ = __contains__ = _no  # type: ignore[assignment]


# What a step gives that did not run.
NOTHING = _Nothing()


class ExpressionError(ValueError):
    """A template is not valid, or refers to a value that does not exist."""


def has_template(value: Any) -> bool:
    """Return whether any text in a JSON value holds a template."""
    if isinstance(value, str):
        return "{{" in value or "{%" in value
    if isinstance(value, list):
        return any(has_template(item) for item in value)
    if isinstance(value, dict):
        return any(has_template(item) for item in value.values())
    return False


def render(value: Any, context: Mapping[str, Any]) -> Any:
    """Return a JSON value with every template in it filled in.

    Raise ``ExpressionError`` if a template cannot be filled in.
    """
    if isinstance(value, str):
        return _render_text(value, context)
    if isinstance(value, list):
        return [render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    return value


def _render_text(text: str, context: Mapping[str, Any]) -> Any:
    if not has_template(text):
        return text
    whole = _WHOLE_EXPRESSION_RE.match(text)
    try:
        if whole is None:
            return _env.from_string(text).render(**context)
        result = _env.compile_expression(whole["expression"], undefined_to_none=False)(
            **context
        )
    except jinja2.TemplateError as e:
        raise ExpressionError(f"{text!r}: {e}") from e
    except Exception as e:
        # An expression can fail in any way a Python expression can.
        raise ExpressionError(f"{text!r}: {type(e).__name__}: {e}") from e
    if isinstance(result, jinja2.Undefined):
        raise ExpressionError(f"{text!r}: {NO_SUCH_VALUE}")
    return result


def referenced_names(value: Any) -> set[str]:
    """Return the names that the templates in a JSON value refer to.

    These are the first parts: ``{{ find.count }}`` refers to ``find``.
    Raise ``ExpressionError`` if a template is not valid.
    """
    if isinstance(value, str):
        if not has_template(value):
            return set()
        try:
            return jinja2.meta.find_undeclared_variables(_env.parse(value))
        except jinja2.TemplateError as e:
            raise ExpressionError(f"{value!r}: {e}") from e
    names: set[str] = set()
    if isinstance(value, list):
        for item in value:
            names |= referenced_names(item)
    elif isinstance(value, dict):
        for item in value.values():
            names |= referenced_names(item)
    return names
