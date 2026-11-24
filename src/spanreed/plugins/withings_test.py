import asyncio
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.withings import AuthenticationFlow, UserConfig
from spanreed.plugin import Plugin
from spanreed.plugins.withings import WithingsPlugin
from spanreed.test_utils import (
    FakeRedis,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows.model import TriggerSpec, Workflow
from spanreed.workflows.store import WorkflowStore

CONFIG_KEY = "config:plugin:name=withings:user_id=4"


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


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    """Give the plugin config and the workflows one empty Redis."""
    fake = FakeRedis()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
    ):
        yield fake


def _plugin_and_user(config: UserConfig | None) -> tuple[WithingsPlugin, Any]:
    Plugin.reset_registry()
    plugin = WithingsPlugin()
    user = mock_user_find_by_id(4)
    if config is not None:
        asyncio.run(WithingsPlugin.set_config(user, config))
    return plugin, user


def _login(config: UserConfig) -> Any:
    """Return a stand-in for the Withings login that gives ``config``."""
    done = asyncio.Event()
    done.set()
    flow = MagicMock(spec=AuthenticationFlow)
    flow.get_done_event = AsyncMock(return_value=done)
    flow.get_url.return_value = "https://example.invalid/auth"
    flow.get_user_config.return_value = config

    @asynccontextmanager
    async def start(_user: Any) -> Any:
        yield flow

    return patch(
        "spanreed.plugins.withings.WithingsApi.start_authentication", new=start
    )


def _said(mock_bot: AsyncMock) -> list[str]:
    return [call.args[0] for call in mock_bot.send_message.await_args_list]


def test_the_plugin_publishes_the_withings_trigger() -> None:
    plugin, _ = _plugin_and_user(None)

    assert [block.id for block in plugin.blocks()] == ["withings.new-measurements"]


def test_a_config_saved_with_the_old_setting_gives_the_login(redis: FakeRedis) -> None:
    plugin, user = _plugin_and_user(_config())
    # As the plugin saved it while it wrote the daily note itself.
    saved = asyncio.run(WithingsPlugin.get_saved_config_data(user))
    assert saved is not None
    saved["daily_note_fields"] = {"weight": True, "fat_mass": False}
    asyncio.run(redis.set(CONFIG_KEY, json.dumps(saved)))

    assert asyncio.run(WithingsPlugin.get_config(user)) == _config()


@patch_user_interaction("spanreed.plugins.withings")
def test_setup_logs_in_and_points_to_the_recipe(mock_bot: AsyncMock) -> None:
    plugin, user = _plugin_and_user(None)

    with _login(_config(access_token="new")):
        asyncio.run(WithingsPlugin.ask_for_user_config(user))

    assert asyncio.run(WithingsPlugin.get_config(user)) == _config(access_token="new")
    mock_bot.request_user_choice.assert_not_called()
    link, hint = _said(mock_bot)
    assert "https://example.invalid/auth" in link
    assert "“Withings → daily note”" in hint and "“Manage workflows”" in hint


@patch_user_interaction("spanreed.plugins.withings")
def test_setup_again_can_keep_the_login(mock_bot: AsyncMock) -> None:
    plugin, user = _plugin_and_user(_config())
    mock_bot.request_user_choice.return_value = 0  # Keep the current login

    with patch("spanreed.plugins.withings.WithingsApi.start_authentication") as start:
        asyncio.run(WithingsPlugin.ask_for_user_config(user))

    start.assert_not_called()
    assert asyncio.run(WithingsPlugin.get_config(user)) == _config()


@patch_user_interaction("spanreed.plugins.withings")
def test_setup_again_can_log_in_again(mock_bot: AsyncMock) -> None:
    plugin, user = _plugin_and_user(_config())
    mock_bot.request_user_choice.return_value = 1  # Log in again

    with _login(_config(access_token="new")):
        asyncio.run(WithingsPlugin.ask_for_user_config(user))

    assert asyncio.run(WithingsPlugin.get_config(user)).access_token == "new"


@patch_user_interaction("spanreed.plugins.withings")
def test_setup_says_nothing_of_recipes_to_a_user_who_has_the_workflow(
    mock_bot: AsyncMock,
) -> None:
    plugin, user = _plugin_and_user(_config())
    mock_bot.request_user_choice.return_value = 0  # Keep the current login
    workflow = Workflow(
        id="wf_1",
        user_id=4,
        name="Withings → daily note",
        trigger=TriggerSpec(block="withings.new-measurements", params={}),
        steps=[],
    )
    asyncio.run(WorkflowStore().save(workflow))

    asyncio.run(WithingsPlugin.ask_for_user_config(user))

    assert _said(mock_bot) == []


def test_token_refresh_saves_the_new_tokens() -> None:
    plugin, user = _plugin_and_user(_config())
    config = asyncio.run(WithingsPlugin.get_config(user))
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
        refreshed = asyncio.run(AuthenticationFlow.refresh_token(user, config))
        saved = asyncio.run(WithingsPlugin.get_config(user))

    assert refreshed == saved == _config(access_token="fresh", refresh_token="r2")


# --- Secrets and health values stay out of the log ----------------------------


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.text = str(payload)
    return response


TOKEN_BODY = {
    "userid": "u",
    "access_token": "ACCESS-SECRET",
    "refresh_token": "REFRESH-SECRET",
    "expires_in": "3600",
    "scope": "s",
    "token_type": "Bearer",
}
MEASURE_RESPONSE = {
    "status": 0,
    "body": {
        "updatetime": 1794995597,
        "measuregrps": [
            {
                "grpid": 8823953042,
                "attrib": 0,
                "date": 1794987283,
                "modified": 1794987327,
                "measures": [{"value": 64344, "type": 1, "unit": -3}],
            }
        ],
    },
}


def test_token_refresh_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    plugin, user = _plugin_and_user(_config(refresh_token="OLD-REFRESH-SECRET"))
    config = asyncio.run(WithingsPlugin.get_config(user))
    caplog.set_level("DEBUG")

    with (
        patch(
            "spanreed.apis.withings.requests.post",
            return_value=_response({"status": 0, "body": TOKEN_BODY}),
        ),
        patch.object(
            AuthenticationFlow,
            "get_client_secret",
            new=AsyncMock(return_value="CLIENT-SECRET"),
        ),
    ):
        asyncio.run(AuthenticationFlow.refresh_token(user, config))

    assert "Withings token refresh: status 0" in caplog.text
    assert "SECRET" not in caplog.text


def test_login_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    plugin, user = _plugin_and_user(None)
    caplog.set_level("DEBUG")

    async def run() -> None:
        flow = AuthenticationFlow(user)
        await flow.get_done_event()
        await flow.authenticate("AUTH-CODE-SECRET")

    with (
        patch(
            "spanreed.apis.withings.requests.post",
            return_value=_response({"status": 0, "body": TOKEN_BODY}),
        ),
        patch.object(
            AuthenticationFlow,
            "get_client_secret",
            new=AsyncMock(return_value="CLIENT-SECRET"),
        ),
    ):
        asyncio.run(run())

    assert "Withings token request: status 0" in caplog.text
    assert "SECRET" not in caplog.text


def test_get_measurements_logs_no_token_and_no_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from spanreed.apis.withings import WithingsApi

    user = mock_user_find_by_id(4)
    api = WithingsApi(user, _config(access_token="ACCESS-SECRET"))
    caplog.set_level("DEBUG")

    with patch(
        "spanreed.apis.withings.requests.post",
        return_value=_response(MEASURE_RESPONSE),
    ):
        measurements = asyncio.run(api.get_measurements())

    assert measurements
    assert "SECRET" not in caplog.text
    # The log names the groups, but holds none of the measured values.
    assert "8823953042" in caplog.text
    assert "updatetime=1794995597" in caplog.text
    assert "64344" not in caplog.text
