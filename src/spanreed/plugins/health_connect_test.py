import asyncio
import datetime
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.health_connect import HealthConnectApi, HealthConnectStore
from spanreed.apis.health_connect_blocks import DayChanged, RawRecordsChanged
from spanreed.plugin import Plugin
from spanreed.plugins.health_connect import (
    HealthConnectPlugin,
    UserConfig,
    normalize_vault_folder,
    setup_instructions,
)
from spanreed.test_utils import (
    FakeRedis,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows import store as workflow_store
from spanreed.workflows.recipes import all_recipes

UTC = datetime.timezone.utc
DATE = datetime.date(2026, 5, 9)


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    """Give the store, the plugin config and the workflows one empty Redis."""
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.health_connect.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
    ):
        yield fake


@pytest.fixture(autouse=True)
def asked_for_birth_date() -> Iterator[AsyncMock]:
    """Stand in for the date of birth question of the setup flow."""
    with patch(
        "spanreed.plugins.health_connect.ask_for_birth_date",
        new=AsyncMock(return_value=True),
    ) as mock:
        yield mock


def _plugin() -> HealthConnectPlugin:
    Plugin.reset_registry()
    return HealthConnectPlugin()


def _steps_record(record_id: str, hour: int, count: int) -> dict:
    """Return a steps record of DATE as the bridge app sends it."""
    return {
        "count": count,
        "start_time": f"2026-05-09T{hour:02d}:00:00Z",
        "end_time": f"2026-05-09T{hour:02d}:10:00Z",
        "metadata": {"id": record_id},
    }


def test_name() -> None:
    plugin = _plugin()

    assert plugin.name() == "Health Connect"
    assert plugin.canonical_name() == "health-connect"


def test_the_plugin_publishes_its_triggers() -> None:
    assert [block.id for block in _plugin().blocks()] == [
        "health-connect.day-changed",
        "health-connect.raw-records-changed",
    ]


# -- setup ---------------------------------------------------------------------


@patch_user_interaction("spanreed.plugins.health_connect")
def test_setup_generates_registers_and_shows_a_token(mock_bot: AsyncMock) -> None:
    _plugin()
    user = mock_user_find_by_id(4)

    asyncio.run(HealthConnectPlugin.ask_for_user_config(user))
    config = asyncio.run(HealthConnectPlugin.get_config(user))

    assert len(config.token) >= 40
    # The token the HTTP route will look up maps back to this user.
    assert asyncio.run(HealthConnectApi.user_id_for_token(config.token)) == 4
    # A first setup has no token to keep, so it asks nothing.
    mock_bot.request_user_choice.assert_not_awaited()
    instructions, hint = [c.args[0] for c in mock_bot.send_message.await_args_list]
    assert config.token in instructions
    assert "https://spanreed.ink/health-connect" in instructions
    # The plugin only stores the data. The user is told what writes it.
    assert "“Health data → daily note”" in hint
    assert "“Manage workflows”" in hint


@patch_user_interaction("spanreed.plugins.health_connect")
def test_setup_does_not_point_at_the_recipes_when_a_workflow_is_there(
    mock_bot: AsyncMock,
) -> None:
    _plugin()
    user = mock_user_find_by_id(4)
    workflow = all_recipes()["health-connect-daily-note"].workflow_for(4)
    asyncio.run(workflow_store.WorkflowStore().save(workflow))

    asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    (instructions,) = [c.args[0] for c in mock_bot.send_message.await_args_list]
    assert "Custom header" in instructions


@patch_user_interaction("spanreed.plugins.health_connect")
def test_setup_asks_for_the_timezone_when_it_is_not_set(mock_bot: AsyncMock) -> None:
    _plugin()
    user = mock_user_find_by_id(4)
    user.timezone = None

    async def set_timezone(asked_user: Any, bot: Any) -> bool:
        asked_user.timezone = "Asia/Jerusalem"
        return True

    with patch(
        "spanreed.plugins.health_connect.ask_for_timezone", side_effect=set_timezone
    ) as ask:
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    ask.assert_awaited_once()
    assert asyncio.run(HealthConnectPlugin.get_config(user)).token


@patch_user_interaction("spanreed.plugins.health_connect")
def test_setup_stops_when_the_timezone_is_not_valid(mock_bot: AsyncMock) -> None:
    _plugin()
    user = mock_user_find_by_id(4)
    user.timezone = None

    with patch(
        "spanreed.plugins.health_connect.ask_for_timezone",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    assert asyncio.run(HealthConnectPlugin.get_saved_config_data(user)) is None
    assert "Set your timezone" in mock_bot.send_message.await_args.args[0]


@patch_user_interaction("spanreed.plugins.health_connect")
def test_setup_does_not_ask_for_a_timezone_that_is_set(mock_bot: AsyncMock) -> None:
    _plugin()
    user = mock_user_find_by_id(4)

    with patch("spanreed.plugins.health_connect.ask_for_timezone") as ask:
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    ask.assert_not_called()


def test_setup_asks_for_a_missing_birth_date_and_goes_on_without_it(
    asked_for_birth_date: AsyncMock,
) -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    asked_for_birth_date.return_value = False

    with patch_user_interaction_in(plugin) as mock_bot:
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    asked_for_birth_date.assert_awaited_once()
    messages = [c.args[0] for c in mock_bot.send_message.await_args_list]
    assert "Heart rate zone minutes are skipped" in messages[0]
    assert asyncio.run(HealthConnectPlugin.get_config(user)).token


def test_setup_does_not_ask_for_a_birth_date_that_is_set(
    asked_for_birth_date: AsyncMock,
) -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    user.birth_date = datetime.date(1990, 4, 23)

    with patch_user_interaction_in(plugin):
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    asked_for_birth_date.assert_not_awaited()


@patch_user_interaction("spanreed.plugins.health_connect")
def test_reconfigure_keeps_the_token(mock_bot: AsyncMock) -> None:
    _plugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 0  # Keep the current token
    asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))
    asyncio.run(HealthConnectApi.register_token(user, "abc"))

    asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    assert asyncio.run(HealthConnectPlugin.get_config(user)) == UserConfig(token="abc")
    assert asyncio.run(HealthConnectApi.user_id_for_token("abc")) == 4
    # The token did not change, so the setup instructions are not sent again.
    messages = [c.args[0] for c in mock_bot.send_message.await_args_list]
    assert not any("Custom header" in message for message in messages)


@patch_user_interaction("spanreed.plugins.health_connect")
def test_reconfigure_can_replace_the_token(mock_bot: AsyncMock) -> None:
    _plugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 1  # Generate a new token
    asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))
    asyncio.run(HealthConnectApi.register_token(user, "abc"))

    asyncio.run(HealthConnectPlugin.ask_for_user_config(user))
    config = asyncio.run(HealthConnectPlugin.get_config(user))

    assert config.token not in ("", "abc")
    assert asyncio.run(HealthConnectApi.user_id_for_token(config.token)) == 4
    # The old token no longer works.
    assert asyncio.run(HealthConnectApi.user_id_for_token("abc")) is None
    assert config.token in mock_bot.send_message.await_args_list[0].args[0]


@patch_user_interaction("spanreed.plugins.health_connect")
def test_show_setup_resends_instructions(mock_bot: AsyncMock) -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))

    asyncio.run(plugin._show_setup(user))

    sent = mock_bot.send_message.await_args
    assert "<code>abc</code>" in sent.args[0]
    assert sent.kwargs == {"parse_html": True}


def test_setup_instructions_put_the_bare_token_in_code() -> None:
    text = setup_instructions("tok<en>")

    # Telegram copies a whole <code> block on tap, so the token is alone.
    assert "<code>tok&lt;en&gt;</code>" in text
    assert "<code>Bearer</code>" in text


def test_a_config_saved_by_the_old_plugin_still_loads(redis: FakeRedis) -> None:
    _plugin()
    user = mock_user_find_by_id(4)
    saved = {
        "token": "abc",
        "raw_data_folder": "Data/raw",
        "daily_note_fields": {"steps": False},
    }
    asyncio.run(
        redis.set("config:plugin:name=health-connect:user_id=4", json.dumps(saved))
    )

    # The folder and the properties are settings of the workflows now.
    assert asyncio.run(HealthConnectPlugin.get_config(user)) == UserConfig(token="abc")


def test_a_user_with_no_config_has_an_empty_token() -> None:
    _plugin()

    config = asyncio.run(HealthConnectPlugin.get_config(mock_user_find_by_id(4)))

    assert config == UserConfig(token="")


def test_normalize_vault_folder() -> None:
    assert normalize_vault_folder("/Data//Health Connect/ ") == "Data/Health Connect"
    assert normalize_vault_folder("Data\\raw") == "Data/raw"
    for bad in ("", " / ", "../outside", "a/./b"):
        with pytest.raises(ValueError):
            normalize_vault_folder(bad)


# -- payloads ------------------------------------------------------------------


def test_ingest_stores_records_and_summarizes() -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    raw = json.dumps(
        {
            "timestamp": "2026-05-09T14:00:00Z",
            "app_version": "1.2.3",
            "heart_rate": [
                {"bpm": 72, "time": "2026-05-09T08:15:00Z"},
                {"bpm": "bad", "time": "2026-05-09T08:16:00Z"},
            ],
            "resting_heart_rate": [{"bpm": 58, "time": "2026-05-09T06:00:00Z"}],
            "steps": [
                {
                    "count": 120,
                    "start_time": "2026-05-09T08:00:00Z",
                    "end_time": "2026-05-09T08:10:00Z",
                }
            ],
            "sleep": [{"start_time": "x", "end_time": "y"}],
        }
    )

    summary = asyncio.run(plugin._ingest(user, raw))
    # The mock user's timezone is UTC, so the store buckets by UTC date.
    store = HealthConnectStore(user)
    hr = asyncio.run(store.get_heart_rate(DATE))
    rhr = asyncio.run(store.get_resting_heart_rate(DATE))
    steps = asyncio.run(store.get_steps(DATE))

    assert summary.heart_rate_samples == 1
    assert summary.resting_heart_rate_samples == 1
    assert summary.steps_records == 1
    assert summary.skipped_records == 1
    assert summary.touched_dates == {DATE}
    assert summary.metric_dates == {DATE}
    assert [s.bpm for s in hr] == [72]
    assert [s.bpm for s in rhr] == [58]
    assert [r.count for r in steps] == [120]


def test_ingest_empty_payload_touches_nothing(redis: FakeRedis) -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)

    summary = asyncio.run(
        plugin._ingest(user, json.dumps({"timestamp": "2026-05-09T14:00:00Z"}))
    )

    assert summary.touched_dates == set()
    assert redis.hashes == {}


def test_ingest_stores_unknown_types_and_extra_fields() -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    raw = json.dumps(
        {
            "timestamp": "2026-05-10T14:00:00Z",
            "heart_rate": [
                {"bpm": 72, "time": "2026-05-09T08:15:00Z", "source": "watch"}
            ],
            "sleep": [{"session_end_time": "2026-05-09T06:00:00Z", "stages": [1, 2]}],
            "weight": [{"kilograms": 70.5}],
        }
    )

    summary = asyncio.run(plugin._ingest(user, raw))
    store = HealthConnectStore(user, tz=UTC)
    hr = asyncio.run(store.get_raw_records("heart-rate", DATE))
    sleep = asyncio.run(store.get_raw_records("sleep", DATE))
    # No timestamp in the record: filed under the day it was received.
    weight = asyncio.run(store.get_raw_records("weight", datetime.date(2026, 5, 10)))

    assert hr == [{"bpm": 72, "time": "2026-05-09T08:15:00Z", "source": "watch"}]
    assert sleep == [{"session_end_time": "2026-05-09T06:00:00Z", "stages": [1, 2]}]
    assert weight == [{"kilograms": 70.5}]
    # Only heart rate is a type that the day's metrics are computed from.
    assert summary.metric_dates == {DATE}
    assert summary.touched_dates == {DATE, datetime.date(2026, 5, 10)}


def test_ingest_applies_deletions() -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    kept, gone = _steps_record("keep", 8, 100), _steps_record("gone", 9, 250)
    first = json.dumps({"timestamp": "2026-05-09T14:00:00Z", "steps": [kept, gone]})
    entries = [{"type": "steps", "id": "gone"}, {"type": "sleep", "id": "unknown"}]
    second = json.dumps({"timestamp": "2026-05-10T14:00:00Z", "deleted": entries})

    asyncio.run(plugin._ingest(user, first))
    with patch.object(plugin._logger, "info") as log_info:
        summary = asyncio.run(plugin._ingest(user, second))
    logged = "\n".join(call.args[0] for call in log_info.call_args_list)
    steps = asyncio.run(HealthConnectStore(user).get_steps(DATE))

    assert [record.count for record in steps] == [100]
    assert summary.deleted_records == 1
    assert summary.unmatched_deletions == 1
    # The step total of the day changed, so its metrics must be written again.
    assert summary.metric_dates == {DATE}
    assert set(summary.stored_dates) == {"steps", "deleted", "deleted-records"}
    assert "1 deleted on the phone (1 deleted ids not found)" in logged
    # ``deleted`` is not named as a health record type.
    assert "no metrics" not in logged


def test_the_metadata_of_a_payload_is_not_stored_as_a_record_type() -> None:
    plugin = _plugin()
    raw = json.dumps(
        {
            "timestamp": "2026-05-09T14:00:00Z",
            "app_version": "1.2.3",
            "timezone": "Asia/Kolkata",
            "steps": [_steps_record("a", 8, 100)],
        }
    )

    summary = asyncio.run(plugin._ingest(mock_user_find_by_id(4), raw))

    assert set(summary.stored_dates) == {"steps"}


def test_a_payload_is_announced_to_the_workflows_as_the_days_it_changed() -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    raw = json.dumps(
        {
            "timestamp": "2026-05-10T14:00:00Z",
            "heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}],
            "steps": [_steps_record("a", 8, 100)],
            "sleep": [{"session_end_time": "2026-05-08T06:00:00Z"}],
            "weight": [{"kilograms": 70.5}],
        }
    )

    with (
        patch.object(DayChanged, "emit", new=AsyncMock()) as days,
        patch.object(RawRecordsChanged, "emit", new=AsyncMock()) as raw_records,
    ):
        summary = asyncio.run(plugin._ingest(user, raw))
        asyncio.run(plugin._announce(user, summary))

    # A day for the metrics: sleep and weight are not metrics.
    days.assert_awaited_once_with(user, ["2026-05-09"])
    # A day of each record type for the raw records.
    raw_records.assert_awaited_once_with(
        user,
        [
            "heart-rate|2026-05-09",
            "sleep|2026-05-08",
            "steps|2026-05-09",
            "weight|2026-05-10",
        ],
    )


def test_run_for_user_survives_a_bad_payload(redis: FakeRedis) -> None:
    """A malformed body is logged and skipped; the next one is still drained."""
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    good = json.dumps({"heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}]})

    class StopLoop(Exception):
        pass

    # Deliveries in order: a bad one, a good one, then stop the loop by
    # simulating cancellation at the next poll.
    deliveries: list[str | Exception] = ["{not json", good, StopLoop()]

    async def fake_dequeue(_user: object) -> str:
        item = deliveries.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))
    with (
        patch.object(HealthConnectApi, "dequeue_payload", side_effect=fake_dequeue),
        patch.object(plugin, "_announce", new=AsyncMock()) as announce,
    ):
        with pytest.raises(StopLoop):
            asyncio.run(plugin.run_for_user(user))

    # Startup re-registers the configured token.
    assert asyncio.run(HealthConnectApi.user_id_for_token("abc")) == 4
    # The good payload after the bad one was stored and announced.
    stored = redis.hashes["health-connect:4:heart-rate:2026-05-09"]
    assert list(stored.values()) == ['{"bpm":72,"time":"2026-05-09T08:15:00Z"}']
    announce.assert_awaited_once()


# -- deleting stored data --------------------------------------------------------


def _day_keys(fake: FakeRedis) -> set[str]:
    return {key for key in fake.hashes if key.startswith("health-connect:4:")}


def _seed_days(user: Any, days: int) -> datetime.date:
    """Store a heart rate record on each of the last ``days`` days and today."""
    today = datetime.datetime.now(tz=UTC).date()
    for age in range(days + 1):
        day = today - datetime.timedelta(days=age)
        record = {"bpm": 70, "time": f"{day.isoformat()}T08:00:00Z"}
        asyncio.run(HealthConnectStore(user).add_records("heart_rate", [record]))
    return today


@pytest.mark.parametrize("choice, days", [(0, 1), (1, 7), (2, 30)])
def test_delete_data_removes_the_chosen_days(
    choice: int, days: int, redis: FakeRedis
) -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    _seed_days(user, 40)

    with patch_user_interaction_in(plugin) as mock_bot:
        mock_bot.request_user_choice.side_effect = [choice, 0]  # range, Delete
        asyncio.run(plugin._delete_data(user))

    assert len(_day_keys(redis)) == 41 - days
    confirm = mock_bot.request_user_choice.await_args_list[1].args[0]
    assert "Daily notes and raw data files are not changed" in confirm


@pytest.mark.parametrize("answers", [[3], [1, 1]])  # Cancel range / Cancel confirm
def test_delete_data_cancel_deletes_nothing(
    answers: list[int], redis: FakeRedis
) -> None:
    plugin = _plugin()
    user = mock_user_find_by_id(4)
    _seed_days(user, 3)

    with patch_user_interaction_in(plugin) as mock_bot:
        mock_bot.request_user_choice.side_effect = answers
        asyncio.run(plugin._delete_data(user))

    assert len(_day_keys(redis)) == 4
    mock_bot.send_message.assert_not_awaited()


def patch_user_interaction_in(plugin: HealthConnectPlugin) -> Any:
    """Patch the plugin module's ``UserInteractionApi`` with a mock facade."""
    patcher = patch("spanreed.plugins.health_connect.UserInteractionApi", autospec=True)

    class _Patched:
        def __enter__(self) -> AsyncMock:
            mock_bot = patcher.start()
            mock_bot.for_user = AsyncMock(return_value=mock_bot)
            mock_bot.request_user_choice = AsyncMock()
            mock_bot.send_message = AsyncMock()
            return mock_bot

        def __exit__(self, *args: object) -> None:
            patcher.stop()

    return _Patched()
