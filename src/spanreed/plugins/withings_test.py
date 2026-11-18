import asyncio
import datetime
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.withings import (
    AuthenticationFlow,
    DailyNoteFields,
    MeasurementType,
    UserConfig,
)
from spanreed.plugin import Plugin
from spanreed.plugins.withings import WithingsPlugin
from spanreed.test_utils import (
    FakeRedis,
    mock_user_find_by_id,
    patch_obsidian,
    patch_user_interaction,
)

DATE = datetime.date(2026, 5, 9)
NOTE = "Daily/2026-05-09.md"
MEASUREMENTS = {
    DATE: {
        MeasurementType.WEIGHT: 80.5,
        MeasurementType.FAT_PERCENTAGE: 20.1,
        MeasurementType.FAT_MASS: 16.2,
        MeasurementType.HEART_PULSE: 60,
    }
}


MEASUREMENTS_AS_PROPERTIES = {
    "weight": 80.5,
    "fat-percentage": 20.1,
    "fat-mass": 16.2,
    "heart-pulse": 60,
}


def _config(**kwargs: Any) -> UserConfig:
    values: dict[str, Any] = {
        "userid": "u",
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": "3600",
        "scope": "s",
        "token_type": "Bearer",
        **kwargs,
    }
    return UserConfig(**values)


# The fixtures' objects for the running test. The patch decorators on the
# tests hide the test signature from pytest, so a test cannot take a fixture
# as an argument.
CURRENT: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    fake = FakeRedis()
    CURRENT["redis"] = fake
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.withings.redis_api", new=fake),
    ):
        yield fake


@pytest.fixture(autouse=True)
def withings_api() -> Iterator[AsyncMock]:
    """Stand in for the Withings API, which returns ``MEASUREMENTS``."""
    api = AsyncMock()
    api.get_measurements.return_value = MEASUREMENTS
    CURRENT["withings_api"] = api
    with patch(
        "spanreed.plugins.withings.WithingsApi.for_user",
        new=AsyncMock(return_value=api),
    ):
        yield api


def _plugin_and_user(config: UserConfig | None) -> tuple[WithingsPlugin, Any]:
    Plugin.reset_registry()
    plugin = WithingsPlugin()
    user = mock_user_find_by_id(4)
    if config is not None:
        asyncio.run(WithingsPlugin.set_config(user, config))
    return plugin, user


def _written(mock_obsidian: AsyncMock) -> dict[str, Any]:
    return {
        call.args[1]: call.args[2]
        for call in mock_obsidian.set_value_of_property.await_args_list
    }


def test_config_saved_before_the_setting_writes_every_property() -> None:
    config = _config()

    assert config.daily_note_fields == DailyNoteFields()
    assert DailyNoteFields.properties() == [
        "weight",
        "fat-percentage",
        "fat-mass",
        "heart-pulse",
    ]
    loaded = _config(daily_note_fields={"weight": False, "gone": True})
    assert loaded.daily_note_fields == DailyNoteFields(weight=False)


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_sync_writes_every_measurement_by_default(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    plugin, user = _plugin_and_user(_config())
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.return_value = None

    written = asyncio.run(plugin._sync(user))

    assert written == 4
    assert _written(mock_obsidian) == {
        "weight": 80.5,
        "fat-percentage": 20.1,
        "fat-mass": 16.2,
        "heart-pulse": 60,
    }


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_sync_writes_only_the_properties_turned_on(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    fields = DailyNoteFields(fat_mass=False, heart_pulse=False)
    plugin, user = _plugin_and_user(_config(daily_note_fields=fields))
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.return_value = None

    written = asyncio.run(plugin._sync(user))

    assert written == 2
    assert _written(mock_obsidian) == {"weight": 80.5, "fat-percentage": 20.1}
    # A property that is off is not read from the note either.
    read = [call.args[1] for call in mock_obsidian.get_property.await_args_list]
    assert read == ["weight", "fat-percentage"]


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_nothing_is_fetched_when_every_property_is_off(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    fields = DailyNoteFields(
        weight=False, fat_percentage=False, fat_mass=False, heart_pulse=False
    )
    plugin, user = _plugin_and_user(_config(daily_note_fields=fields))

    assert asyncio.run(plugin._sync(user)) == 0
    asyncio.run(plugin._sync_now(user))

    mock_obsidian.set_value_of_property.assert_not_called()
    mock_bot.request_user_choice.assert_not_called()
    assert "turned off" in mock_bot.send_message.await_args.args[0]


@patch_user_interaction("spanreed.plugins.withings")
def test_reconfigure_changes_the_properties_and_keeps_the_login(
    mock_bot: AsyncMock,
) -> None:
    plugin, user = _plugin_and_user(_config())
    # Keep the current login; choose properties; turn fat-mass off; Done.
    mock_bot.request_user_choice.side_effect = [0, 1, 2, 4]

    with patch("spanreed.plugins.withings.WithingsApi.start_authentication") as start:
        asyncio.run(WithingsPlugin.ask_for_user_config(user))
        config = asyncio.run(WithingsPlugin.get_config(user))

    start.assert_not_called()
    assert config == _config(daily_note_fields=DailyNoteFields(fat_mass=False))


@patch_user_interaction("spanreed.plugins.withings")
def test_logging_in_again_keeps_the_chosen_properties(mock_bot: AsyncMock) -> None:
    fields = DailyNoteFields(heart_pulse=False)
    plugin, user = _plugin_and_user(_config(daily_note_fields=fields))
    # Log in again; keep the properties.
    mock_bot.request_user_choice.side_effect = [1, 0]
    done = asyncio.Event()
    done.set()
    flow = MagicMock(spec=AuthenticationFlow)
    flow.get_done_event = AsyncMock(return_value=done)
    flow.get_url.return_value = "https://example.invalid/auth"
    flow.get_user_config.return_value = _config(access_token="new")

    @asynccontextmanager
    async def start(_user: Any) -> Any:
        yield flow

    with patch("spanreed.plugins.withings.WithingsApi.start_authentication", new=start):
        asyncio.run(WithingsPlugin.ask_for_user_config(user))
        config = asyncio.run(WithingsPlugin.get_config(user))

    assert config.access_token == "new"
    assert config.daily_note_fields == fields


def test_token_refresh_keeps_the_latest_choice_of_properties() -> None:
    plugin, user = _plugin_and_user(_config())
    stale = asyncio.run(WithingsPlugin.get_config(user))
    # The user unchecks a property after the sync loaded its config.
    asyncio.run(
        WithingsPlugin.set_config(
            user, _config(daily_note_fields=DailyNoteFields(weight=False))
        )
    )
    response = MagicMock()
    response.json.return_value = {
        "status": 0,
        "body": {
            "userid": "u",
            "access_token": "fresh",
            "refresh_token": "r2",
            "expires_in": "3600",
            "scope": "s",
            "token_type": "Bearer",
        },
    }

    with (
        patch("spanreed.apis.withings.requests.post", return_value=response),
        patch.object(
            AuthenticationFlow, "get_client_secret", new=AsyncMock(return_value="x")
        ),
    ):
        refreshed = asyncio.run(AuthenticationFlow.refresh_token(user, stale))
        saved = asyncio.run(WithingsPlugin.get_config(user))

    assert refreshed.access_token == "fresh"
    assert saved.access_token == "fresh"
    assert saved.daily_note_fields == DailyNoteFields(weight=False)


# --- A measurement is written to a note once only -----------------------------


class FakeNote:
    """Daily note properties, kept in memory behind the Obsidian mock."""

    def __init__(self, mock_obsidian: AsyncMock) -> None:
        self.properties: dict[str, Any] = {}
        mock_obsidian.get_daily_note.return_value = NOTE
        mock_obsidian.get_property.side_effect = self._get
        mock_obsidian.set_value_of_property.side_effect = self._set

    async def _get(self, _note: str, name: str) -> Any:
        return self.properties.get(name)

    async def _set(self, _note: str, name: str, value: Any) -> None:
        self.properties[name] = value


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_a_property_the_user_removed_is_not_written_again(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    plugin, user = _plugin_and_user(_config())
    note = FakeNote(mock_obsidian)

    assert asyncio.run(plugin._sync(user)) == 4
    # Withings still returns the measurement, but the user removes it.
    del note.properties["weight"]
    del note.properties["fat-mass"]

    assert asyncio.run(plugin._sync(user)) == 0
    assert asyncio.run(plugin._sync(user)) == 0
    assert set(note.properties) == {"fat-percentage", "heart-pulse"}
    # The hourly sync says nothing about it.
    assert not [
        call for call in mock_bot.notify.await_args_list if "removed" in call.args[0]
    ]


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_manual_sync_reports_the_measurements_it_left_out(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    plugin, user = _plugin_and_user(_config())
    note = FakeNote(mock_obsidian)
    asyncio.run(plugin._sync(user))
    del note.properties["weight"]
    mock_bot.request_user_choice.return_value = 0  # Today

    asyncio.run(plugin._sync_now(user))

    assert "weight" not in note.properties
    messages = [call.args[0] for call in mock_bot.notify.await_args_list]
    assert "Left out 1 measurement that you removed from your notes." in messages


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_a_value_already_in_the_note_counts_as_written(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    """Covers values that were written before Spanreed kept track."""
    plugin, user = _plugin_and_user(_config())
    note = FakeNote(mock_obsidian)
    note.properties.update(MEASUREMENTS_AS_PROPERTIES)

    assert asyncio.run(plugin._sync(user)) == 0
    del note.properties["weight"]

    assert asyncio.run(plugin._sync(user)) == 0
    assert "weight" not in note.properties


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_a_new_reading_is_written_after_the_old_one_was_removed(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    withings_api: AsyncMock = CURRENT["withings_api"]
    plugin, user = _plugin_and_user(_config())
    note = FakeNote(mock_obsidian)
    asyncio.run(plugin._sync(user))
    del note.properties["weight"]
    asyncio.run(plugin._sync(user))
    # The user weighs in again: the day's latest weight is now another value.
    withings_api.get_measurements.return_value = {
        DATE: {**MEASUREMENTS[DATE], MeasurementType.WEIGHT: 81.2}
    }

    assert asyncio.run(plugin._sync(user)) == 1
    assert note.properties["weight"] == 81.2


@patch_obsidian("spanreed.plugins.withings")
@patch_user_interaction("spanreed.plugins.withings")
def test_what_was_written_is_remembered_for_each_user_and_day(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    redis: FakeRedis = CURRENT["redis"]
    plugin, user = _plugin_and_user(_config())
    FakeNote(mock_obsidian)

    asyncio.run(plugin._sync(user))

    key = "withings:4:handled:2026-05-09"
    assert redis.sets[key] == {
        "weight=80.5",
        "fat-percentage=20.1",
        "fat-mass=16.2",
        "heart-pulse=60",
    }
    assert redis.expirations[key] == 90 * 24 * 3600
