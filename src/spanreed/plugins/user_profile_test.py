import asyncio
from unittest.mock import AsyncMock, patch

from spanreed.plugin import Plugin
from spanreed.plugins.user_profile import UserProfilePlugin
from spanreed.test_utils import FakeRedis, patch_user_interaction
from spanreed.user import User


def test_name() -> None:
    Plugin.reset_registry()
    plugin = UserProfilePlugin()

    assert plugin.name() == "User Profile"
    assert plugin.canonical_name() == "user-profile"


def _user() -> User:
    user = User()
    user.id = 4
    user.name = "Test User"
    return user


@patch_user_interaction("spanreed.plugins.user_profile")
def test_set_timezone_stores_valid_name(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = UserProfilePlugin()
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


@patch_user_interaction("spanreed.plugins.user_profile")
def test_set_timezone_shows_current_value(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = UserProfilePlugin()
    fake = FakeRedis()
    user = _user()
    user.timezone = "Europe/London"
    mock_bot.request_user_input.return_value = "Europe/Paris"

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(plugin._set_timezone(user))

    assert "Europe/London" in mock_bot.request_user_input.await_args.args[0]
    assert user.timezone == "Europe/Paris"


@patch_user_interaction("spanreed.plugins.user_profile")
def test_set_timezone_rejects_unknown_name(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = UserProfilePlugin()
    fake = FakeRedis()
    user = _user()
    mock_bot.request_user_input.return_value = "Mars/Olympus_Mons"

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(plugin._set_timezone(user))

    assert user.timezone is None
    assert fake.strings == {}
    assert "not a known timezone" in mock_bot.send_message.await_args.args[0]


def test_existing_users_are_registered_at_startup() -> None:
    from spanreed.apis.telegram_bot import TelegramBotPlugin

    Plugin.reset_registry()
    plugin = UserProfilePlugin()
    old_user, new_user = _user(), _user()
    new_user.id = 5
    telegram = AsyncMock()
    telegram.get_users.return_value = [old_user, new_user]

    async def is_registered(user: User) -> bool:
        return user is new_user

    with (
        patch.object(
            Plugin, "get_plugin_by_class", AsyncMock(return_value=telegram)
        ) as get_plugin,
        patch.object(UserProfilePlugin, "is_registered", side_effect=is_registered),
        patch.object(UserProfilePlugin, "register_user", AsyncMock()) as register,
    ):
        asyncio.run(plugin._register_existing_users())

    get_plugin.assert_awaited_once_with(TelegramBotPlugin)
    register.assert_awaited_once_with(old_user)
