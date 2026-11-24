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

# A text that is exactly one ``{{ ... }}``.
_WHOLE_EXPRESSION_RE = re.compile(
    r"^\s*\{\{(?P<expression>(?:(?!\{\{|\}\}|\{%|%\}).)+)\}\}\s*$", re.DOTALL
)


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
        raise ExpressionError(f"{text!r}: there is no such value.")
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
