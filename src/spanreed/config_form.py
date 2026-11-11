"""Describe a plugin config as a form, and check what the form sends back.

A plugin config is a dataclass. The web UI edits one as a form instead of a
chat flow. This module builds the form description from the type hints of
the dataclass, turns a saved config into the values the form shows, and
validates the values the form submits.

A field can set how the form treats it with ``field(metadata=...)``:

- ``{"config_form": "hidden"}``: never sent to the browser and never changed
  by the form. Use it for a value that only the plugin's own setup may set.
- ``{"config_form": "secret"}``: the saved value is not sent to the browser.
  The form shows an empty password box, and an empty box keeps the saved
  value.
- ``{"config_form": "plain"}``: an ordinary field, even if its name looks
  like a secret.
- ``{"config_help": "..."}``: a line of help text under the field.

Without ``config_form``, a field whose name looks like a secret (``token``,
``api_key``, ``password``...) is a secret if it is a string outside a list,
and hidden otherwise.
"""

from __future__ import annotations

import dataclasses
import re
import types
import typing
from typing import Any

FORM_KEY = "config_form"
HELP_KEY = "config_help"

_SECRET_NAME_RE = re.compile(
    r"token|secret|password|passwd|api_?key|credential|auth", re.IGNORECASE
)
_NONE_TYPE = type(None)


class ConfigFormError(ValueError):
    """A submitted value does not fit the config.

    ``path`` holds the field names and list positions that lead to the bad
    value, outermost first.
    """

    def __init__(self, path: list[str | int], message: str) -> None:
        """Create an error for the value at ``path``."""
        self.path = path
        self.message = message
        where = " › ".join(_label(str(part)) for part in path)
        super().__init__(f"{where}: {message}" if where else message)


def _label(name: str) -> str:
    """Return a field name as a label: ``raw_data_folder`` → ``Raw data folder``."""
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _is_dataclass_type(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _split_optional(tp: Any) -> tuple[Any, bool]:
    """Return a type without its ``None`` alternative, and if it had one."""
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(tp) if arg is not _NONE_TYPE]
        if len(args) < len(typing.get_args(tp)):
            if len(args) == 1:
                return args[0], True
            return typing.Union[tuple(args)], True
    return tp, False


def _default_of(field: dataclasses.Field) -> tuple[bool, Any]:
    """Return whether a field has a default, and the default as JSON data."""
    if field.default is not dataclasses.MISSING:
        value = field.default
    elif field.default_factory is not dataclasses.MISSING:
        value = field.default_factory()
    else:
        return False, None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return True, value


def _field_mode(field: dataclasses.Field, node: dict[str, Any], in_list: bool) -> str:
    """Return ``"hidden"``, ``"secret"`` or ``"plain"`` for a field."""
    mode = field.metadata.get(FORM_KEY)
    if mode is None:
        if not _SECRET_NAME_RE.search(field.name):
            return "plain"
        mode = "secret"
    if mode == "secret" and (node["kind"] != "string" or in_list):
        # Only a string outside a list can keep its saved value when the
        # form leaves it empty.
        return "hidden"
    return str(mode)


def _node(tp: Any, in_list: bool) -> dict[str, Any]:
    """Return the form description of one type."""
    tp, optional = _split_optional(tp)
    origin = typing.get_origin(tp)
    node: dict[str, Any]
    if tp is bool:
        node = {"kind": "boolean"}
    elif tp is str:
        node = {"kind": "string"}
    elif tp is int:
        node = {"kind": "integer"}
    elif tp is float:
        node = {"kind": "number"}
    elif origin is typing.Literal and all(
        isinstance(choice, str) for choice in typing.get_args(tp)
    ):
        node = {"kind": "choice", "choices": list(typing.get_args(tp))}
    elif origin is list and len(typing.get_args(tp)) == 1:
        item = _node(typing.get_args(tp)[0], in_list=True)
        node = {"kind": "list", "item": item, "blank": _blank(item)}
    elif _is_dataclass_type(tp):
        node = {"kind": "object", "fields": _fields(tp, in_list)}
    else:
        # Anything else is edited as JSON text.
        node = {"kind": "json", "mapping": tp is dict or origin is dict}
    if optional:
        node["optional"] = True
        if node["kind"] == "object":
            node["blank"] = _blank({**node, "optional": False})
    return node


def _fields(config_class: Any, in_list: bool) -> list[dict[str, Any]]:
    hints = typing.get_type_hints(config_class)
    fields = []
    for field in dataclasses.fields(config_class):
        if not field.init:
            continue
        node = _node(hints[field.name], in_list)
        mode = _field_mode(field, node, in_list)
        has_default, default = _default_of(field)
        node.update(name=field.name, label=_label(field.name), mode=mode)
        node["required"] = not has_default
        if has_default:
            node["default"] = default
        if HELP_KEY in field.metadata:
            node["help"] = str(field.metadata[HELP_KEY])
        fields.append(node)
    return fields


def _blank(node: dict[str, Any]) -> Any:
    """Return the value a new, empty form field of this kind starts with."""
    if node.get("optional"):
        return None
    kind = node["kind"]
    if kind == "string":
        return ""
    if kind == "boolean":
        return False
    if kind == "choice":
        return node["choices"][0] if node["choices"] else None
    if kind == "list":
        return []
    if kind == "object":
        return {
            field["name"]: field["default"] if "default" in field else _blank(field)
            for field in node["fields"]
            if field["mode"] != "hidden"
        }
    if kind == "json":
        return {} if node["mapping"] else None
    return None  # a number: the box starts empty


def _public(node: dict[str, Any]) -> dict[str, Any]:
    """Return a form description without what the browser must not get."""
    public = {key: value for key, value in node.items() if key != "default"}
    if node["kind"] == "object":
        public["fields"] = [
            _public(field) for field in node["fields"] if field["mode"] != "hidden"
        ]
    elif node["kind"] == "list":
        public["item"] = _public(node["item"])
    return public


def _full_schema(config_class: Any) -> dict[str, Any]:
    if not _is_dataclass_type(config_class):
        raise TypeError(f"{config_class!r} is not a dataclass.")
    return _node(config_class, in_list=False)


def form_schema(config_class: Any) -> dict[str, Any]:
    """Return the form description of a config dataclass, as JSON data.

    Leave out hidden fields and the defaults of the fields.
    """
    return _public(_full_schema(config_class))


def _shown(node: dict[str, Any], value: Any) -> Any:
    """Return a saved value in the shape the form expects."""
    if value is None and node.get("optional"):
        return None
    kind = node["kind"]
    if kind == "object":
        saved = value if isinstance(value, dict) else {}
        shown = {}
        for field in node["fields"]:
            if field["mode"] == "hidden":
                continue
            if field["mode"] == "secret":
                shown[field["name"]] = ""
            elif field["name"] in saved:
                shown[field["name"]] = _shown(field, saved[field["name"]])
            elif "default" in field:
                shown[field["name"]] = _shown(field, field["default"])
            else:
                shown[field["name"]] = _blank(field)
        return shown
    if kind == "list":
        items = value if isinstance(value, list) else []
        return [_shown(node["item"], item) for item in items]
    return value


def form_value(config_class: Any, saved: dict[str, Any] | None) -> dict[str, Any]:
    """Return the values the form shows for a saved config.

    ``saved`` is the config as JSON data, or ``None`` if none is saved. Fill
    in the default of a field that is not saved. Leave out hidden fields,
    and show a secret as an empty string.
    """
    shown: dict[str, Any] = _shown(_full_schema(config_class), saved)
    return shown


def _checked(
    node: dict[str, Any], value: Any, saved: Any, path: list[str | int]
) -> Any:
    """Return a submitted value as JSON data for the config, or raise."""
    if value is None:
        if node.get("optional"):
            return None
        raise ConfigFormError(path, "A value is required.")
    kind = node["kind"]
    if kind == "string":
        if not isinstance(value, str):
            raise ConfigFormError(path, "Expected text.")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ConfigFormError(path, "Expected yes or no.")
        return value
    if kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigFormError(path, "Expected a number.")
        if kind == "number":
            return value
        if isinstance(value, float) and not value.is_integer():
            raise ConfigFormError(path, "Expected a whole number.")
        return int(value)
    if kind == "choice":
        if value not in node["choices"]:
            raise ConfigFormError(path, "Not one of the allowed values.")
        return value
    if kind == "list":
        if not isinstance(value, list):
            raise ConfigFormError(path, "Expected a list.")
        return [
            _checked(node["item"], item, None, [*path, index + 1])
            for index, item in enumerate(value)
        ]
    if kind == "object":
        return _checked_object(node, value, saved, path)
    if node["mapping"] and not isinstance(value, dict):
        raise ConfigFormError(path, "Expected a JSON object.")
    return value


def _checked_object(
    node: dict[str, Any], value: Any, saved: Any, path: list[str | int]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigFormError(path, "Expected a group of fields.")
    saved = saved if isinstance(saved, dict) else {}
    known = {field["name"] for field in node["fields"] if field["mode"] != "hidden"}
    for name in value:
        if name not in known:
            raise ConfigFormError([*path, str(name)], "Unknown field.")

    checked = {}
    for field in node["fields"]:
        name = field["name"]
        field_path = [*path, name]
        keep_saved = field["mode"] == "hidden" or (
            field["mode"] == "secret" and value.get(name) in (None, "")
        )
        if keep_saved:
            if name in saved:
                checked[name] = saved[name]
            elif "default" in field:
                checked[name] = field["default"]
            elif field["mode"] == "hidden":
                raise ConfigFormError(
                    field_path,
                    "This value is not set yet. Set the plugin up from a "
                    "chat first.",
                )
            else:
                raise ConfigFormError(field_path, "A value is required.")
        elif name in value:
            checked[name] = _checked(field, value[name], saved.get(name), field_path)
        elif "default" in field:
            checked[name] = field["default"]
        else:
            raise ConfigFormError(field_path, "A value is required.")
    return checked


def config_data_from_form(
    config_class: Any, submitted: Any, saved: dict[str, Any] | None
) -> dict[str, Any]:
    """Return the config to save, as JSON data, for a submitted form.

    Check every submitted value against the type of its field. Take a hidden
    field, and a secret that the form left empty, from ``saved``. Raise
    ``ConfigFormError`` if a value does not fit.
    """
    return _checked_object(_full_schema(config_class), submitted, saved, [])
