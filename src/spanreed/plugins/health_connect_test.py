import asyncio
import datetime
import json
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.health_connect import HealthConnectApi, HealthConnectStore
from spanreed.plugin import Plugin
from spanreed.plugins.health_connect import (
    HealthConnectPlugin,
    UserConfig,
    setup_instructions,
)
from spanreed.test_utils import (
    FakeRedis,
    mock_user_find_by_id,
    patch_user_interaction,
)


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
    assert list(stored.values()) == ["72"]


def test_setup_instructions_put_the_bare_token_in_code() -> None:
    text = setup_instructions("tok-en_1")
    assert "<code>tok-en_1</code>" in text
    assert "`" not in text
