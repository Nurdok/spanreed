import asyncio
from unittest.mock import AsyncMock, patch

from spanreed.plugin import Plugin
from spanreed.plugins.plugin_manager import PluginManagerPlugin
from spanreed.test_utils import FakeRedis, patch_user_interaction
from spanreed.user import User


def _user() -> User:
    user = User()
    user.id = 4
    user.name = "Test User"
    return user


@patch_user_interaction("spanreed.plugins.plugin_manager")
def test_set_timezone_stores_valid_name(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = PluginManagerPlugin()
    fake = FakeRedis()
    user = _user()
    mock_bot.request_user_input.return_value = "Asia/Jerusalem"

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(plugin._set_timezone(user))

    assert user.timezone == "Asia/Jerusalem"
    assert fake.strings["user:4:timezone"] == "Asia/Jerusalem"
    # The prompt tells the user it isn't set yet.
    assert "not set" in mock_bot.request_user_input.await_args.args[0]
    confirmation = mock_bot.send_message.await_args.args[0]
    assert "Asia/Jerusalem" in confirmation


@patch_user_interaction("spanreed.plugins.plugin_manager")
def test_set_timezone_shows_current_value(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = PluginManagerPlugin()
    fake = FakeRedis()
    user = _user()
    user.timezone = "Europe/London"
    mock_bot.request_user_input.return_value = "Europe/Paris"

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(plugin._set_timezone(user))

    assert "Europe/London" in mock_bot.request_user_input.await_args.args[0]
    assert user.timezone == "Europe/Paris"


@patch_user_interaction("spanreed.plugins.plugin_manager")
def test_set_timezone_rejects_unknown_name(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = PluginManagerPlugin()
    fake = FakeRedis()
    user = _user()
    mock_bot.request_user_input.return_value = "Mars/Olympus_Mons"

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(plugin._set_timezone(user))

    assert user.timezone is None
    assert fake.strings == {}
    assert "not a known timezone" in mock_bot.send_message.await_args.args[0]
