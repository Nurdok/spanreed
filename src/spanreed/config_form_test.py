import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

import pytest

from spanreed.config_form import (
    ConfigFormError,
    config_data_from_form,
    form_schema,
    form_value,
)
from spanreed.plugins import (
    gmail_monitor,
    health_connect,
)
from spanreed.apis import withings


@dataclass
class Inner:
    name: str
    count: int = 1


@dataclass
class Config:
    title: str
    api_key: str
    ratio: float = 0.5
    enabled: bool = True
    note: Optional[str] = None
    mode: Literal["fast", "slow"] = "fast"
    items: List[Inner] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    inner: Inner | None = None
    internal: str = field(default="x", metadata={"config_form": "hidden"})
    auth: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for index, item in enumerate(self.items):
            if isinstance(item, dict):
                self.items[index] = Inner(**item)


SAVED = {
    "title": "T",
    "api_key": "s3cret",
    "ratio": 0.25,
    "enabled": False,
    "note": None,
    "mode": "slow",
    "items": [{"name": "a", "count": 2}],
    "extra": {"k": [1, 2]},
    "inner": None,
    "internal": "keep-me",
    "auth": {"blob": "keep-me-too"},
}


def _fields(schema: dict) -> dict[str, dict]:
    return {f["name"]: f for f in schema["fields"]}


def test_form_schema_describes_each_kind_of_field() -> None:
    fields = _fields(form_schema(Config))

    assert {name: f["kind"] for name, f in fields.items()} == {
        "title": "string",
        "api_key": "string",
        "ratio": "number",
        "enabled": "boolean",
        "note": "string",
        "mode": "choice",
        "items": "list",
        "extra": "json",
        "inner": "object",
    }
    assert fields["title"]["label"] == "Title"
    assert fields["title"]["required"] is True
    assert fields["note"]["optional"] is True
    assert fields["mode"]["choices"] == ["fast", "slow"]
    assert fields["items"]["item"]["kind"] == "object"
    assert fields["items"]["blank"] == {"name": "", "count": 1}
    assert fields["inner"]["blank"] == {"name": "", "count": 1}
    # A secret by its name; a hidden field and a non-text secret are left out.
    assert fields["api_key"]["mode"] == "secret"
    assert "internal" not in fields
    assert "auth" not in fields


def test_form_schema_is_json_and_never_carries_defaults() -> None:
    text = json.dumps(form_schema(Config))

    assert '"default"' not in text


def test_form_value_masks_secrets_and_leaves_out_hidden_fields() -> None:
    value = form_value(Config, SAVED)

    assert value == {
        "title": "T",
        "api_key": "",
        "ratio": 0.25,
        "enabled": False,
        "note": None,
        "mode": "slow",
        "items": [{"name": "a", "count": 2}],
        "extra": {"k": [1, 2]},
        "inner": None,
    }
    assert "s3cret" not in json.dumps(value)
    assert "keep-me" not in json.dumps(value)


def test_form_value_fills_in_defaults_for_what_is_not_saved() -> None:
    assert form_value(Config, None) == {
        "title": "",
        "api_key": "",
        "ratio": 0.5,
        "enabled": True,
        "note": None,
        "mode": "fast",
        "items": [],
        "extra": {},
        "inner": None,
    }
    assert form_value(Config, {"title": "T", "items": [{"name": "a"}]})["items"] == [
        {"name": "a", "count": 1}
    ]


def test_config_data_keeps_hidden_fields_and_an_empty_secret() -> None:
    submitted = form_value(Config, SAVED)
    submitted["title"] = "New"
    submitted["inner"] = {"name": "n", "count": 3.0}

    data = config_data_from_form(Config, submitted, SAVED)

    assert data == {**SAVED, "title": "New", "inner": {"name": "n", "count": 3}}
    assert isinstance(data["inner"]["count"], int)
    # The result loads the same way a saved config does.
    assert Config(**data).items == [Inner("a", 2)]


def test_config_data_replaces_a_secret_that_was_typed() -> None:
    submitted = {**form_value(Config, SAVED), "api_key": "new-key"}

    assert config_data_from_form(Config, submitted, SAVED)["api_key"] == "new-key"


def test_config_data_uses_defaults_for_fields_the_form_did_not_send() -> None:
    data = config_data_from_form(Config, {"title": "T", "api_key": "k"}, None)

    assert data["ratio"] == 0.5
    assert data["internal"] == "x"
    assert data["auth"] == {}


@pytest.mark.parametrize(
    "change, path",
    [
        ({"title": 5}, ["title"]),
        ({"title": None}, ["title"]),
        ({"ratio": "high"}, ["ratio"]),
        ({"enabled": 1}, ["enabled"]),
        ({"mode": "medium"}, ["mode"]),
        ({"items": {}}, ["items"]),
        ({"items": [{"name": "a", "count": 1.5}]}, ["items", 1, "count"]),
        ({"items": [{"name": "a"}, {"count": 2}]}, ["items", 2, "name"]),
        ({"extra": [1]}, ["extra"]),
        ({"inner": {"name": "n", "bogus": 1}}, ["inner", "bogus"]),
        # A hidden field cannot be set through the form.
        ({"internal": "hack"}, ["internal"]),
        ({"auth": {}}, ["auth"]),
    ],
)
def test_config_data_rejects_values_that_do_not_fit(change: dict, path: list) -> None:
    submitted = {**form_value(Config, SAVED), **change}

    with pytest.raises(ConfigFormError) as error:
        config_data_from_form(Config, submitted, SAVED)

    assert error.value.path == path


def test_config_data_requires_a_secret_that_was_never_saved() -> None:
    with pytest.raises(ConfigFormError) as error:
        config_data_from_form(Config, {"title": "T", "api_key": ""}, None)

    assert error.value.path == ["api_key"]
    assert str(error.value) == "Api key: A value is required."


def test_config_data_rejects_a_form_that_is_not_an_object() -> None:
    with pytest.raises(ConfigFormError):
        config_data_from_form(Config, None, SAVED)


@pytest.mark.parametrize(
    "config_class",
    [
        gmail_monitor.UserConfig,
        health_connect.UserConfig,
        withings.UserConfig,
    ],
)
def test_real_plugin_configs_have_a_form(config_class: Any) -> None:
    schema = form_schema(config_class)

    json.dumps(schema)
    assert schema["kind"] == "object"


@dataclasses.dataclass
class _Step:
    kind: str
    options: dict[str, Any]


@dataclasses.dataclass
class _Recipe:
    name: str
    steps: list[_Step]
    enabled: bool = True


@dataclasses.dataclass
class _Cookbook:
    recipes: list[_Recipe]
    servings: int = 2


def test_a_config_of_lists_of_groups_round_trips_through_the_form() -> None:
    config = _Cookbook(
        recipes=[
            _Recipe(name="Soup", steps=[_Step(kind="boil", options={"minutes": 20})])
        ],
        servings=4,
    )
    saved = dataclasses.asdict(config)

    data = config_data_from_form(_Cookbook, form_value(_Cookbook, saved), saved)

    assert data == saved


def test_gmail_monitor_form_has_nothing_to_edit() -> None:
    # The Gmail login is not kept in the config, and the rules are workflows.
    assert form_schema(gmail_monitor.UserConfig)["fields"] == []
    assert config_data_from_form(gmail_monitor.UserConfig, {}, {}) == {}


def test_health_connect_form_has_nothing_to_edit_and_keeps_the_token() -> None:
    saved = {"token": "abc"}
    schema = form_schema(health_connect.UserConfig)

    # The token is the only setting, and only the setup may set it.
    assert schema["fields"] == []
    data = config_data_from_form(health_connect.UserConfig, {}, saved)
    assert data == {"token": "abc"}
    with pytest.raises(ConfigFormError):
        config_data_from_form(health_connect.UserConfig, {"token": "stolen"}, saved)


def test_withings_form_has_nothing_to_edit_and_keeps_the_login() -> None:
    saved = {
        "userid": "u",
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": "3600",
        "scope": "s",
        "token_type": "Bearer",
    }
    schema = form_schema(withings.UserConfig)

    # Only the login flow sets the tokens, so there is nothing to show.
    assert schema["fields"] == []
    assert "access_token" not in json.dumps(form_value(withings.UserConfig, saved))
    assert config_data_from_form(withings.UserConfig, {}, saved) == saved
    with pytest.raises(ConfigFormError):
        config_data_from_form(withings.UserConfig, {"access_token": "stolen"}, saved)
