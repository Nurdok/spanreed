"""Move values between JSON data and the dataclasses of blocks."""

from __future__ import annotations

import dataclasses
import datetime
import types
from typing import Any, TypeVar

from spanreed.config_form import ConfigFormError, config_data_from_form

T = TypeVar("T")

# How much of a value a run's trace keeps.
TRACE_MAX_TEXT = 2000
TRACE_MAX_ITEMS = 50


def from_data(cls: type[T], data: Any) -> T:
    """Build the dataclass ``cls`` from JSON data, checking each value.

    The check is the one that the settings form applies to a plugin config.
    Raise ``ValueError`` if a value does not fit, or if ``cls`` rejects the
    values in its ``__post_init__``.
    """
    try:
        checked = config_data_from_form(cls, data, None)
    except ConfigFormError as e:
        raise ValueError(str(e)) from e
    return cls(**checked)


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
