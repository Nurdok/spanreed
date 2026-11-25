"""Move values between JSON data and the dataclasses of blocks."""

from __future__ import annotations

import dataclasses
import datetime
import re
import types
import typing
from typing import Any, TypeVar

from spanreed.config_form import ConfigFormError, config_data_from_form

T = TypeVar("T")

# How much of a value a run's trace keeps.
TRACE_MAX_TEXT = 2000
TRACE_MAX_ITEMS = 50


def from_data(cls: type[T], data: Any, written: Any = None) -> T:
    """Build the dataclass ``cls`` from JSON data, checking each value.

    The check is the one that the settings form applies to a plugin config.
    Raise ``ValueError`` if a value does not fit, or if ``cls`` rejects the
    values in its ``__post_init__``.

    ``written`` is the data as it is stored, before its templates were
    filled in. With it, the error says what was written and what that came
    to: "Expected yes or no" does not tell a user which value was neither,
    or what to do about it.
    """
    try:
        checked = config_data_from_form(cls, data, None)
    except ConfigFormError as e:
        raise ValueError(_explained(cls, e, data, written)) from e
    return cls(**checked)


# What stands for a value that is not there, where ``None`` is a value.
_MISSING = object()
# How much of a text an error quotes.
_QUOTED_MAX = 80
# A text that is one template and nothing else: its value keeps its type.
_ONE_TEMPLATE_RE = re.compile(r"\s*\{\{(?P<asked>(?:(?!\{\{|\}\}).)+)\}\}\s*", re.S)


def _at(data: Any, path: list[str | int]) -> Any:
    """Return the value at a path of names and positions, if there is one."""
    for part in path:
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and isinstance(part, int) and part < len(data):
            data = data[part]
        else:
            return _MISSING
    return data


def _quoted(text: str) -> str:
    """Return a text in quotes, and only its start if it is long."""
    if len(text) > _QUOTED_MAX:
        text = text[: _QUOTED_MAX - 1] + "…"
    return f"“{text}”"


def in_words(value: Any) -> str:
    """Return a value as a user would call it: ``the number 3``."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"the number {value}"
    if isinstance(value, str):
        return f"the text {_quoted(value)}" if value else "an empty text"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"a list of {len(value)}" if value else "an empty list"
    if (
        isinstance(value, (dict, types.SimpleNamespace))
        or dataclasses.is_dataclass(value)
        and not isinstance(value, type)
    ):
        # All that a step or a trigger gives, where one of its values is meant.
        return "a group of values"
    return _quoted(str(value))


def type_in_words(tp: Any) -> str:
    """Return a type as a user would call it: ``whole number``.

    For what a block gives: the type of an output, shown next to its name.
    """
    if tp is Any:
        return "any value"
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        kinds = [type_in_words(arg) for arg in args if arg is not type(None)]
        words = " or ".join(dict.fromkeys(kinds))
        return f"{words}, or nothing" if len(kinds) < len(args) else words
    if origin in (list, tuple, set, frozenset) or tp in (list, tuple, set, frozenset):
        item = args[0] if args else Any
        if item is Any:
            return "list"
        if isinstance(item, type) and dataclasses.is_dataclass(item):
            return "list of items"
        return f"list of {type_in_words(item)}s"
    if tp is bool:
        return "yes or no"
    if tp is int:
        return "whole number"
    if tp is float:
        return "number"
    if tp is str:
        return "text"
    if tp is bytes:
        return "file content"
    if tp is datetime.datetime:
        return "date and time"
    if tp is datetime.date:
        return "date"
    if tp is type(None):
        return "nothing"
    return "group of values"


def _question_of(value: Any, asked: str) -> str:
    """Return a template that asks something of a value, as an example."""
    if value is None:
        return f"{{{{ {asked} is not none }}}}"
    if isinstance(value, (int, float)):
        return f"{{{{ {asked} > 0 }}}}"
    if isinstance(value, str):
        return f"{{{{ {asked} != '' }}}}"
    return f"{{{{ {asked} | length > 0 }}}}"


def _asks_yes_or_no(cls: type[Any], error: ConfigFormError) -> bool:
    """Return whether the value that does not fit has to be yes or no."""
    if error.message == "Expected yes or no.":
        return True
    # Nothing where a value is needed: the message does not say of which
    # kind. The type of the field does, for a field of the class itself.
    if error.message != "A value is required." or len(error.path) != 1:
        return False
    try:
        return typing.get_type_hints(cls).get(str(error.path[0])) is bool
    except Exception:
        return False


def _explained(cls: type[Any], error: ConfigFormError, data: Any, written: Any) -> str:
    """Return what a value that does not fit is, and how to put it right."""
    value = _at(data, error.path)
    if value is _MISSING:
        return str(error)
    where = str(error).removesuffix(error.message).removesuffix(": ")
    start = f"{where}: " if where else ""
    # What was written, if it is a template: its value is known in a run.
    text: str | None = None
    if written is not None:
        stored = _at(written, error.path)
        if isinstance(stored, str) and ("{{" in stored or "{%" in stored):
            text = stored
    if not _asks_yes_or_no(cls, error):
        if text is None:
            return str(error)
        return f"{start}{_quoted(text)} came to {in_words(value)}. {error.message}"

    if text is None:
        # Not from a template: the value itself is neither.
        return f"{start}{in_words(value)} is neither yes nor no."
    one = _ONE_TEMPLATE_RE.fullmatch(text)
    if one is not None:
        advice = (
            "Ask something of it that is true or false, such as "
            f"{_quoted(_question_of(value, one['asked'].strip()))}."
        )
    elif text.count("{{") == 1 and "{%" not in text:
        # ``{{ count }} > 0``: what is outside the braces is not worked
        # out. It is text, and so is the whole.
        inside = " ".join(text.replace("{{", " ").replace("}}", " ").split())
        advice = (
            "Only what is inside the braces is worked out. Put all of it "
            f"inside: {_quoted('{{ ' + inside + ' }}')}."
        )
    else:
        advice = (
            "A text is never yes or no. Put all of what is asked inside one "
            "pair of braces, such as “{{ step.count > 0 and step.found }}”."
        )
    return (
        f"{start}{_quoted(text)} came to {in_words(value)}, which is neither "
        f"yes nor no. {advice}"
    )


def to_data(value: Any) -> Any:
    """Return a value as JSON data, for a run's trace.

    Keep the start of a long text and of a long list. Show a value that has
    no JSON form by its ``repr``.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > TRACE_MAX_TEXT:
            return value[:TRACE_MAX_TEXT] + "…"
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_data(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, types.SimpleNamespace):
        return {name: to_data(item) for name, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): to_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [to_data(item) for item in list(value)[:TRACE_MAX_ITEMS]]
        if len(value) > TRACE_MAX_ITEMS:
            items.append(f"… and {len(value) - TRACE_MAX_ITEMS} more")
        return items
    return to_data(repr(value))


def whole_data(value: Any) -> Any:
    """Return a value as JSON data with nothing cut off.

    This is for an event that is kept to run a workflow for again:
    ``to_data`` keeps only the start of a long text or list, and a workflow
    that ran for that would write the shortened value.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: whole_data(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, types.SimpleNamespace):
        return {name: whole_data(item) for name, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): whole_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [whole_data(item) for item in value]
    return repr(value)


def from_whole_data(data: Any) -> Any:
    """Return what ``whole_data`` gave as a value that templates can read.

    A mapping becomes an object with a field for each name, so that
    ``{{ trigger.date }}`` reads it as it reads the event it was made from.
    """
    if isinstance(data, dict):
        return types.SimpleNamespace(
            **{str(name): from_whole_data(item) for name, item in data.items()}
        )
    if isinstance(data, list):
        return [from_whole_data(item) for item in data]
    return data


class NotPlain(ValueError):
    """A value cannot be made again from JSON data as what it was."""


def plain_data(value: Any, cls: type[Any] | None = None) -> dict[str, Any]:
    """Return an object of plain values as JSON data, to make it again.

    This is for what a run keeps so that it can go on after a restart:
    the event it runs for, and what each step gave. ``value`` is a
    dataclass, and ``make_again`` gives one that is the same in every
    field. ``cls`` is the class that ``make_again`` will be given.

    Raise ``NotPlain`` if a field holds more than JSON has: ``None``,
    numbers, texts, and lists and mappings of those. Bytes, a date or an
    object of a class would come back as something else, and the steps
    that go on would work with the wrong thing. Raise it too if the value
    is not of the class ``cls``.
    """
    if cls is not None and type(value) is not cls:
        raise NotPlain(f"{type(value).__name__}, not {cls.__name__}")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, types.SimpleNamespace):
        return {name: _plain(item) for name, item in vars(value).items()}
    raise NotPlain(type(value).__name__)


def _plain(value: Any) -> Any:
    # The exact types: a subclass may hold more than JSON keeps.
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is list:
        return [_plain(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise NotPlain("a mapping with a key that is not a text")
        return {key: _plain(item) for key, item in value.items()}
    raise NotPlain(type(value).__name__)


def make_again(cls: type[Any], data: Any) -> Any:
    """Return the object that ``plain_data`` gave ``data`` for.

    ``cls`` is its class. An object without one, such as the event of a
    test, comes back as an object with a field for each name. Raise
    ``NotPlain`` if the class no longer takes these fields.
    """
    if not isinstance(data, dict):
        raise NotPlain("not the data of an object")
    if cls is types.SimpleNamespace or not dataclasses.is_dataclass(cls):
        return types.SimpleNamespace(**data)
    try:
        return cls(**data)
    except (TypeError, ValueError) as e:
        raise NotPlain(str(e)) from e
