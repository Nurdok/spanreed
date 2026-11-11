import asyncio
import datetime
import json
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.health_connect import (
    HealthConnectApi,
    HealthConnectStore,
    HeartRateSample,
    StepsRecord,
)
from spanreed.plugin import Plugin
from spanreed.plugins.health_connect import (
    HealthConnectPlugin,
    UserConfig,
    raw_file_path,
    setup_instructions,
)
from spanreed.test_utils import (
    FakeRedis,
    mock_user_find_by_id,
    patch_obsidian,
    patch_user_interaction,
)

UTC = datetime.timezone.utc
DATE = datetime.date(2026, 5, 9)
NOTE = "Daily/2026-05-09.md"


def _seed_steps(fake: FakeRedis, user: object, *counts: int) -> None:
    """Store one step interval per count on DATE, each starting an hour apart."""
    start = datetime.datetime(2026, 5, 9, 8, 0, tzinfo=UTC)
    records = [
        StepsRecord(
            start + datetime.timedelta(hours=i),
            start + datetime.timedelta(hours=i, minutes=10),
            count,
        )
        for i, count in enumerate(counts)
    ]
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(HealthConnectStore(user, tz=UTC).add_steps(records))  # type: ignore[arg-type]


def test_name() -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()

    assert plugin.name() == "Health Connect"
    assert plugin.canonical_name() == "health-connect"


@patch_user_interaction("spanreed.plugins.health_connect")
def test_ask_for_user_config_generates_registers_and_shows_token(
    mock_bot: AsyncMock,
) -> None:
    Plugin.reset_registry()
    HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)

    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
    ):
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))
        config: UserConfig = asyncio.run(HealthConnectPlugin.get_config(user))
        # The token the HTTP route will look up maps back to this user.
        assert asyncio.run(HealthConnectApi.user_id_for_token(config.token)) == 4

    assert len(config.token) >= 40
    mock_bot.send_message.assert_awaited_once()
    sent_text = mock_bot.send_message.await_args.args[0]
    assert config.token in sent_text
    assert "https://spanreed.ink/health-connect" in sent_text


@patch_user_interaction("spanreed.plugins.health_connect")
def test_ask_for_user_config_asks_for_timezone_when_unset(
    mock_bot: AsyncMock,
) -> None:
    Plugin.reset_registry()
    HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    user.timezone = None

    async def set_timezone(_user: object, _bot: object) -> bool:
        user.timezone = "Asia/Jerusalem"
        return True

    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch(
            "spanreed.plugins.health_connect.ask_for_timezone",
            side_effect=set_timezone,
        ) as mock_ask,
    ):
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))
        config: UserConfig = asyncio.run(HealthConnectPlugin.get_config(user))

    mock_ask.assert_awaited_once()
    assert config.token


@patch_user_interaction("spanreed.plugins.health_connect")
def test_ask_for_user_config_stops_when_timezone_invalid(
    mock_bot: AsyncMock,
) -> None:
    Plugin.reset_registry()
    HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    user.timezone = None

    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch(
            "spanreed.plugins.health_connect.ask_for_timezone",
            new=AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    # No token was generated or registered.
    assert fake.strings == {}


@patch_user_interaction("spanreed.plugins.health_connect")
def test_ask_for_user_config_skips_timezone_prompt_when_set(
    mock_bot: AsyncMock,
) -> None:
    Plugin.reset_registry()
    HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)  # timezone is set to UTC by the helper

    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.health_connect.ask_for_timezone") as mock_ask,
    ):
        asyncio.run(HealthConnectPlugin.ask_for_user_config(user))

    mock_ask.assert_not_called()


@patch_user_interaction("spanreed.plugins.health_connect")
def test_show_setup_resends_instructions(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)

    with patch("spanreed.plugin.redis_api", new=fake):
        asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))
        asyncio.run(plugin._show_setup(user))

    mock_bot.send_message.assert_awaited_once()
    assert mock_bot.send_message.await_args.args[0] == setup_instructions("abc")


def test_ingest_stores_records_and_summarizes() -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
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

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        summary = asyncio.run(plugin._ingest(user, raw))
        # The mock user's timezone is UTC, so the store buckets by UTC date.
        store = HealthConnectStore(user)
        hr = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 9)))
        rhr = asyncio.run(store.get_resting_heart_rate(datetime.date(2026, 5, 9)))
        steps = asyncio.run(store.get_steps(datetime.date(2026, 5, 9)))

    assert summary.heart_rate_samples == 1
    assert summary.resting_heart_rate_samples == 1
    assert summary.steps_records == 1
    assert summary.skipped_records == 1
    assert summary.touched_dates == {datetime.date(2026, 5, 9)}
    assert [s.bpm for s in hr] == [72]
    assert [s.bpm for s in rhr] == [58]
    assert [r.count for r in steps] == [120]


def test_ingest_empty_payload_touches_nothing() -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        summary = asyncio.run(
            plugin._ingest(user, json.dumps({"timestamp": "2026-05-09T14:00:00Z"}))
        )

    assert summary.touched_dates == set()
    assert fake.hashes == {}


def test_run_for_user_survives_a_bad_payload() -> None:
    """A malformed body is logged and skipped; the next one is still drained."""
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
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

    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch.object(HealthConnectApi, "dequeue_payload", side_effect=fake_dequeue),
        patch.object(plugin, "_write_steps", new=AsyncMock(return_value=0)),
        patch.object(plugin, "_write_heart_rate", new=AsyncMock(return_value=1)),
        patch.object(plugin, "_write_raw_files", new=AsyncMock(return_value=1)),
    ):
        asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))
        try:
            asyncio.run(plugin.run_for_user(user))
        except StopLoop:
            pass
        # Startup re-registers the configured token.
        assert asyncio.run(HealthConnectApi.user_id_for_token("abc")) == 4

    # The good payload after the bad one was stored.
    assert len(fake.hashes) == 1
    (stored,) = fake.hashes.values()
    assert list(stored.values()) == ['{"bpm":72,"time":"2026-05-09T08:15:00Z"}']


# --- _write_steps -------------------------------------------------------------


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_steps_writes_total_to_empty_note(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    _seed_steps(fake, user, 120, 30)
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.return_value = None

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_steps(user, [DATE], notify=True))

    assert written == 1
    mock_obsidian.safe_generate_today_note.assert_awaited_once()
    mock_obsidian.get_daily_note.assert_awaited_once_with("Daily", DATE)
    mock_obsidian.set_value_of_property.assert_awaited_once_with(NOTE, "steps", 150)
    assert "150" in mock_bot.notify.await_args.args[0]


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_steps_overwrites_when_total_changed(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    _seed_steps(fake, user, 120, 30)
    mock_obsidian.get_daily_note.return_value = NOTE
    # An earlier, smaller total: steps kept coming, so it must be replaced.
    mock_obsidian.get_property.return_value = 120

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_steps(user, [DATE], notify=False))

    assert written == 1
    mock_obsidian.set_value_of_property.assert_awaited_once_with(NOTE, "steps", 150)
    # Background path: silent.
    mock_bot.notify.assert_not_called()


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_steps_skips_when_total_unchanged(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    _seed_steps(fake, user, 120, 30)
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.return_value = 150

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_steps(user, [DATE], notify=True))

    assert written == 0
    mock_obsidian.set_value_of_property.assert_not_called()


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_steps_skips_dates_without_records(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_steps(user, [DATE], notify=True))

    assert written == 0
    mock_obsidian.get_property.assert_not_called()
    mock_obsidian.set_value_of_property.assert_not_called()


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_steps_no_dates_does_nothing(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)

    assert asyncio.run(plugin._write_steps(user, [], notify=False)) == 0
    mock_obsidian.safe_generate_today_note.assert_not_called()


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_steps_missing_note_is_skipped(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    _seed_steps(fake, user, 120)
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.side_effect = FileNotFoundError("file not found")

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_steps(user, [DATE], notify=True))

    assert written == 0
    mock_obsidian.set_value_of_property.assert_not_called()
    assert "No daily note" in mock_bot.notify.await_args.args[0]


# --- the drain loop writes steps for the dates a payload touched -------------


def test_run_for_user_writes_steps_for_touched_dates() -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    payload = json.dumps(
        {
            "steps": [
                {
                    "count": 100,
                    "start_time": "2026-05-09T08:00:00Z",
                    "end_time": "2026-05-09T08:10:00Z",
                },
                {
                    "count": 200,
                    "start_time": "2026-05-10T08:00:00Z",
                    "end_time": "2026-05-10T08:10:00Z",
                },
            ],
            # Heart rate on another day must not trigger a steps write.
            "heart_rate": [{"bpm": 72, "time": "2026-05-11T08:15:00Z"}],
        }
    )

    class StopLoop(Exception):
        pass

    deliveries: list[str | Exception] = [payload, StopLoop()]

    async def fake_dequeue(_user: object) -> str:
        item = deliveries.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with (
        patch("spanreed.apis.health_connect.redis_api", new=fake),
        patch("spanreed.plugin.redis_api", new=fake),
        patch.object(HealthConnectApi, "dequeue_payload", side_effect=fake_dequeue),
        patch.object(plugin, "_write_steps", new=AsyncMock(return_value=2)) as write,
        patch.object(
            plugin, "_write_heart_rate", new=AsyncMock(return_value=1)
        ) as write_hr,
        patch.object(
            plugin, "_write_raw_files", new=AsyncMock(return_value=3)
        ) as write_raw,
    ):
        asyncio.run(HealthConnectPlugin.set_config(user, UserConfig(token="abc")))
        try:
            asyncio.run(plugin.run_for_user(user))
        except StopLoop:
            pass

    write.assert_awaited_once_with(
        user, [datetime.date(2026, 5, 9), datetime.date(2026, 5, 10)], notify=False
    )
    # Heart rate is written only for the date that had heart rate records.
    write_hr.assert_awaited_once_with(user, [datetime.date(2026, 5, 11)], notify=False)
    write_raw.assert_awaited_once_with(user)


# --- Sync steps command -------------------------------------------------------


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_steps_now_today(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 0  # Today
    today = datetime.datetime.now(tz=UTC).date()

    with patch.object(plugin, "_write_steps", new=AsyncMock(return_value=1)) as write:
        asyncio.run(plugin._sync_steps_now(user))

    write.assert_awaited_once_with(user, [today], notify=True)
    assert "No new" not in mock_bot.notify.await_args.args[0]


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_steps_now_last_week_reports_nothing_new(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 1  # The last week
    today = datetime.datetime.now(tz=UTC).date()

    with patch.object(plugin, "_write_steps", new=AsyncMock(return_value=0)) as write:
        asyncio.run(plugin._sync_steps_now(user))

    assert write.await_args is not None
    dates = write.await_args.args[1]
    assert len(dates) == 7
    assert dates[0] == today - datetime.timedelta(days=6)
    assert dates[-1] == today
    assert "No new step counts" in mock_bot.notify.await_args.args[0]


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_steps_now_cancel(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 2  # Cancel

    with patch.object(plugin, "_write_steps", new=AsyncMock()) as write:
        asyncio.run(plugin._sync_steps_now(user))

    write.assert_not_called()
    mock_bot.notify.assert_not_called()


# --- Sync heart rate command --------------------------------------------------


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_heart_rate_now_today(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 0  # Today
    today = datetime.datetime.now(tz=UTC).date()

    with patch.object(
        plugin, "_write_heart_rate", new=AsyncMock(return_value=1)
    ) as write:
        asyncio.run(plugin._sync_heart_rate_now(user))

    assert mock_bot.request_user_choice.await_args.args[1] == [
        "Today",
        "The last week",
        "All time",
        "Cancel",
    ]
    write.assert_awaited_once_with(user, [today], notify=True)


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_heart_rate_now_all_time_covers_every_stored_date(
    mock_bot: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 2  # All time
    old = datetime.datetime(2025, 1, 3, 12, 0, tzinfo=UTC)
    recent = datetime.datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=UTC)
        asyncio.run(store.add_heart_rate([HeartRateSample(recent, 60)]))
        asyncio.run(store.add_resting_heart_rate([HeartRateSample(old, 50)]))
        with patch.object(
            plugin, "_write_heart_rate", new=AsyncMock(return_value=1)
        ) as write:
            asyncio.run(plugin._sync_heart_rate_now(user))

    # One summary replaces the per-day messages.
    write.assert_awaited_once_with(user, [old.date(), recent.date()], notify=False)
    assert "1 daily note, out of 2 stored days" in mock_bot.notify.await_args.args[0]


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_heart_rate_now_all_time_reports_nothing_new(
    mock_bot: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 2  # All time

    with patch("spanreed.apis.health_connect.redis_api", new=FakeRedis()):
        with patch.object(
            plugin, "_write_heart_rate", new=AsyncMock(return_value=0)
        ) as write:
            asyncio.run(plugin._sync_heart_rate_now(user))

    write.assert_awaited_once_with(user, [], notify=False)
    assert "No new heart rate" in mock_bot.notify.await_args.args[0]


@patch_user_interaction("spanreed.plugins.health_connect")
def test_sync_heart_rate_now_cancel(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)
    mock_bot.request_user_choice.return_value = 3  # Cancel

    with patch.object(plugin, "_write_heart_rate", new=AsyncMock()) as write:
        asyncio.run(plugin._sync_heart_rate_now(user))

    write.assert_not_called()
    mock_bot.notify.assert_not_called()


def test_setup_instructions_put_the_bare_token_in_code() -> None:
    text = setup_instructions("tok-en_1")
    assert "<code>tok-en_1</code>" in text
    assert "`" not in text


# --- Delete data command ------------------------------------------------------


def _day_keys(fake: FakeRedis) -> set[str]:
    return {k for k in fake.hashes if k.startswith("health-connect:4:")}


@pytest.mark.parametrize("choice, days", [(0, 1), (1, 7), (2, 30)])
def test_delete_data_removes_the_chosen_days(choice: int, days: int) -> None:
    @patch_user_interaction("spanreed.plugins.health_connect")
    def run(mock_bot: AsyncMock) -> None:
        Plugin.reset_registry()
        plugin = HealthConnectPlugin()
        fake = FakeRedis()
        user = mock_user_find_by_id(4)
        today = datetime.datetime.now(tz=UTC).date()
        store = HealthConnectStore(user, tz=UTC)
        # One day inside every range, one just outside the 30-day range.
        outside = today - datetime.timedelta(days=30)
        for date in (today, outside):
            noon = datetime.datetime.combine(date, datetime.time(12), tzinfo=UTC)
            with patch("spanreed.apis.health_connect.redis_api", new=fake):
                asyncio.run(store.add_heart_rate([HeartRateSample(noon, 60)]))
                asyncio.run(store.add_resting_heart_rate([HeartRateSample(noon, 50)]))
                asyncio.run(store.add_steps([StepsRecord(noon, noon, 100)]))
        assert len(_day_keys(fake)) == 6
        mock_bot.request_user_choice.side_effect = [choice, 0]  # range, Delete

        with patch("spanreed.apis.health_connect.redis_api", new=fake):
            asyncio.run(plugin._delete_data(user))

        remaining = _day_keys(fake)
        assert len(remaining) == 3
        assert all(outside.isoformat() in k for k in remaining)
        confirm_prompt = mock_bot.request_user_choice.await_args_list[1].args[0]
        first = (today - datetime.timedelta(days=days - 1)).isoformat()
        assert f"from {first} to {today.isoformat()}" in confirm_prompt
        assert "Deleted" in mock_bot.send_message.await_args.args[0]

    run()


@pytest.mark.parametrize("answers", [[3], [1, 1]])  # Cancel range / Cancel confirm
def test_delete_data_cancel_deletes_nothing(answers: list[int]) -> None:
    @patch_user_interaction("spanreed.plugins.health_connect")
    def run(mock_bot: AsyncMock) -> None:
        Plugin.reset_registry()
        plugin = HealthConnectPlugin()
        fake = FakeRedis()
        user = mock_user_find_by_id(4)
        _seed_steps(fake, user, 100)
        before = _day_keys(fake)
        mock_bot.request_user_choice.side_effect = answers

        with patch("spanreed.apis.health_connect.redis_api", new=fake):
            asyncio.run(plugin._delete_data(user))

        assert before and _day_keys(fake) == before
        mock_bot.send_message.assert_not_called()

    run()


# --- Heart rate metrics in the daily note --------------------------------------


def _seed_heart_rate(
    fake: FakeRedis, user: object, bpm: list[int], resting: list[int]
) -> None:
    start = datetime.datetime(2026, 5, 9, 8, 0, tzinfo=UTC)
    store = HealthConnectStore(user, tz=UTC)  # type: ignore[arg-type]
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(
            store.add_heart_rate(
                [
                    HeartRateSample(start + datetime.timedelta(minutes=i), b)
                    for i, b in enumerate(bpm)
                ]
            )
        )
        if resting:
            asyncio.run(
                store.add_resting_heart_rate(
                    [
                        HeartRateSample(start + datetime.timedelta(hours=i), r)
                        for i, r in enumerate(resting)
                    ]
                )
            )


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_heart_rate_writes_all_metrics(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    _seed_heart_rate(fake, user, list(range(60, 161)), resting=[58, 55])
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.return_value = None

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_heart_rate(user, [DATE], notify=True))

    assert written == 1
    calls = [c.args for c in mock_obsidian.set_value_of_property.await_args_list]
    # Resting is the day's last record; 60..160 has p5=65, avg=110, p95=155,
    # p98=158, p99=159.
    assert calls == [
        (NOTE, "resting-heart-rate", 55),
        (NOTE, "heart-rate-p5", 65),
        (NOTE, "heart-rate-avg", 110),
        (NOTE, "heart-rate-p95", 155),
        (NOTE, "heart-rate-p98", 158),
        (NOTE, "heart-rate-p99", 159),
    ]
    assert "heart-rate-avg=110" in mock_bot.notify.await_args.args[0]


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_heart_rate_sets_only_changed_and_present_metrics(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    # No resting record today; p5 is already in the note.
    _seed_heart_rate(fake, user, [70, 80, 90], resting=[])
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.side_effect = lambda _note, prop: (
        71 if prop == "heart-rate-p5" else None
    )

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(plugin._write_heart_rate(user, [DATE], notify=False))

    assert written == 1
    props = [c.args[1] for c in mock_obsidian.set_value_of_property.await_args_list]
    assert props == [
        "heart-rate-avg",
        "heart-rate-p95",
        "heart-rate-p98",
        "heart-rate-p99",
    ]
    mock_bot.notify.assert_not_called()


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_write_heart_rate_skips_days_without_records_or_notes(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    _seed_heart_rate(fake, user, [70], resting=[])
    mock_obsidian.get_daily_note.return_value = NOTE
    mock_obsidian.get_property.side_effect = FileNotFoundError()
    empty_day = DATE + datetime.timedelta(days=1)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        written = asyncio.run(
            plugin._write_heart_rate(user, [DATE, empty_day], notify=True)
        )

    assert written == 0
    mock_obsidian.get_daily_note.assert_awaited_once_with("Daily", DATE)
    assert "No daily note" in mock_bot.notify.await_args.args[0]


# --- Raw data files -----------------------------------------------------------


def _written_files(mock_obsidian: AsyncMock) -> dict[str, str]:
    """Return the text of each file written to the vault, by path."""
    files = {}
    for call in mock_obsidian.write_binary_file.await_args_list:
        assert call.kwargs == {"overwrite": True}
        files[call.args[0]] = call.args[1].decode("utf-8")
    return files


def test_raw_file_path_uses_the_payload_name_of_the_type() -> None:
    assert raw_file_path("resting-heart-rate", DATE) == (
        "Health/raw/resting_heart_rate/2026-05-09.jsonl"
    )


def test_ingest_stores_unknown_types_and_extra_fields() -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
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

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        summary = asyncio.run(plugin._ingest(user, raw))
        store = HealthConnectStore(user, tz=UTC)
        hr = asyncio.run(store.get_raw_records("heart-rate", DATE))
        sleep = asyncio.run(store.get_raw_records("sleep", DATE))
        # No timestamp in the record: filed under the day it was received.
        weight = asyncio.run(
            store.get_raw_records("weight", datetime.date(2026, 5, 10))
        )
        pending = asyncio.run(store.get_export_pending())

    assert hr == [{"bpm": 72, "time": "2026-05-09T08:15:00Z", "source": "watch"}]
    assert sleep == [{"session_end_time": "2026-05-09T06:00:00Z", "stages": [1, 2]}]
    assert weight == [{"kilograms": 70.5}]
    assert summary.heart_rate_dates == {DATE}
    assert summary.steps_dates == set()
    assert summary.touched_dates == {DATE, datetime.date(2026, 5, 10)}
    assert pending == [
        ("heart-rate", DATE),
        ("sleep", DATE),
        ("weight", datetime.date(2026, 5, 10)),
    ]


@patch_obsidian("spanreed.plugins.health_connect")
def test_write_raw_files_writes_each_pending_day_once(
    mock_obsidian: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    raw = json.dumps(
        {
            "heart_rate": [
                {"bpm": 80, "time": "2026-05-09T09:00:00Z"},
                {"bpm": 72, "time": "2026-05-09T08:15:00Z"},
            ],
            "sleep": [{"session_end_time": "2026-05-09T06:00:00Z"}],
        }
    )

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(plugin._ingest(user, raw))
        written = asyncio.run(plugin._write_raw_files(user))
        again = asyncio.run(plugin._write_raw_files(user))

    assert written == 2
    assert again == 0
    # One record on each line, oldest first, as the phone sent it.
    assert _written_files(mock_obsidian) == {
        "Health/raw/heart_rate/2026-05-09.jsonl": (
            '{"bpm":72,"time":"2026-05-09T08:15:00Z"}\n'
            '{"bpm":80,"time":"2026-05-09T09:00:00Z"}\n'
        ),
        "Health/raw/sleep/2026-05-09.jsonl": (
            '{"session_end_time":"2026-05-09T06:00:00Z"}\n'
        ),
    }
    assert mock_obsidian.write_binary_file.await_count == 2


@patch_obsidian("spanreed.plugins.health_connect")
def test_write_raw_files_retries_a_day_that_failed(
    mock_obsidian: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    raw = json.dumps({"heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}]})
    mock_obsidian.write_binary_file.side_effect = [TimeoutError("offline"), None]

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(plugin._ingest(user, raw))
        with pytest.raises(TimeoutError):
            asyncio.run(plugin._write_raw_files(user))
        written = asyncio.run(plugin._write_raw_files(user))

    assert written == 1
    assert mock_obsidian.write_binary_file.await_count == 2


@patch_obsidian("spanreed.plugins.health_connect")
def test_write_raw_files_skips_a_day_with_no_stored_records(
    mock_obsidian: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=UTC)
        asyncio.run(store.mark_export_pending([("heart-rate", DATE)]))
        written = asyncio.run(plugin._write_raw_files(user))
        pending = asyncio.run(store.get_export_pending())

    assert written == 0
    assert pending == []
    mock_obsidian.write_binary_file.assert_not_called()


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_export_raw_now_writes_every_stored_day(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    t = datetime.datetime(2026, 5, 9, 8, 15, tzinfo=UTC)
    epoch_ms = int(t.timestamp() * 1000)
    # Records of the older storage format, with nothing marked as pending.
    fake.hashes["health-connect:4:heart-rate:2026-05-09"] = {str(epoch_ms): "72"}
    fake.hashes["health-connect:4:steps:2026-05-09"] = {
        str(epoch_ms): f"{epoch_ms + 60_000}:15"
    }

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(plugin._export_raw_now(user))

    assert _written_files(mock_obsidian) == {
        "Health/raw/heart_rate/2026-05-09.jsonl": (
            '{"bpm":72,"time":"2026-05-09T08:15:00Z"}\n'
        ),
        "Health/raw/steps/2026-05-09.jsonl": (
            '{"count":15,"start_time":"2026-05-09T08:15:00Z",'
            '"end_time":"2026-05-09T08:16:00Z"}\n'
        ),
    }
    assert "Wrote 2 raw data files" in mock_bot.notify.await_args.args[0]


@patch_obsidian("spanreed.plugins.health_connect")
@patch_user_interaction("spanreed.plugins.health_connect")
def test_export_raw_now_reports_nothing_stored(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = HealthConnectPlugin()
    user = mock_user_find_by_id(4)

    with patch("spanreed.apis.health_connect.redis_api", new=FakeRedis()):
        asyncio.run(plugin._export_raw_now(user))

    mock_obsidian.write_binary_file.assert_not_called()
    assert "No stored Health Connect data" in mock_bot.notify.await_args.args[0]
