import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import InvalidCallbackData

from spanreed.apis.telegram_bot import (
    TelegramBotApi,
    TelegramBotPlugin,
    OUTBOUND_QUEUE_MAX,
)
from spanreed.plugin import Plugin
from spanreed.test_utils import FakeRedis
from spanreed.user import User


def test_stale_callback_data_answered_not_crashed() -> None:
    """A button press from a pre-restart keyboard yields InvalidCallbackData;
    the handler must answer the query instead of raising AttributeError."""
    Plugin.reset_registry()
    plugin = TelegramBotPlugin()

    update = MagicMock()
    query = AsyncMock()
    query.data = InvalidCallbackData()
    update.callback_query = query
    context = MagicMock()

    asyncio.run(plugin.handle_callback_query(update, context))

    query.answer.assert_awaited_once()
    query.delete_message.assert_not_called()


@patch("spanreed.apis.telegram_bot.redis_api", new_callable=FakeRedis)
@patch.object(TelegramBotApi, "get_application")
def test_user_interaction_simple_usage(
    mock_get_application: AsyncMock,
    fake_redis: FakeRedis,
) -> None:
    """Test using the user_interaction context manager from the user's perspective."""

    async def run_test() -> None:
        # Setup mock application
        mock_app = AsyncMock()
        mock_app.bot_data = {}
        mock_get_application.return_value = mock_app

        # Create a bot API instance
        telegram_user_id = 123
        bot_api = TelegramBotApi(telegram_user_id)

        # Create a simple async function that uses user_interaction
        async def send_greeting() -> None:
            async with bot_api.user_interaction():
                await bot_api.send_message("Hello!")

        # Run the function
        await send_greeting()

        # Verify that a message was sent (through the send dispatcher)
        mock_app.bot.send_message.assert_called_once_with(
            chat_id=telegram_user_id, text="Hello!", parse_mode="HTML"
        )

    # Run the async test function
    asyncio.run(run_test())


@patch.object(TelegramBotApi, "get_application")
def test_user_interaction_handles_early_cancellation_properly(
    mock_get_application: AsyncMock,
) -> None:
    """Test that user_interaction properly handles early cancellation without RuntimeError."""

    async def run_test() -> None:
        # Setup mock application
        mock_app = AsyncMock()
        mock_app.bot_data = {}
        mock_get_application.return_value = mock_app

        # Create a bot API instance
        telegram_user_id = 123
        bot_api = TelegramBotApi(telegram_user_id)

        # Mock the methods that would be called during user_interaction setup
        with (
            patch.object(
                bot_api,
                "_add_to_user_interaction_queue",
                new_callable=AsyncMock,
            ),
            patch.object(
                bot_api, "get_user_interaction_lock", new_callable=AsyncMock
            ) as mock_get_lock,
            patch.object(
                bot_api,
                "_try_to_allow_next_user_interaction",
                new_callable=AsyncMock,
            ),
            patch.object(
                bot_api,
                "_remove_from_user_interaction_queue",
                new_callable=AsyncMock,
            ) as mock_remove_queue,
        ):

            # Create a mock lock
            mock_lock = AsyncMock()
            mock_get_lock.return_value = mock_lock

            # Create a mock UserInteraction that raises CancelledError when wait_to_run is called
            with patch(
                "spanreed.apis.telegram_bot.UserInteraction"
            ) as mock_user_interaction_class:
                mock_user_interaction = AsyncMock()
                mock_user_interaction_class.return_value = mock_user_interaction
                # This simulates cancellation during the queue waiting phase
                mock_user_interaction.wait_to_run.side_effect = asyncio.CancelledError(
                    "Test cancellation"
                )

                # After the fix, this should raise CancelledError (not RuntimeError)
                # because the context manager now properly raises instead of returning
                with pytest.raises(asyncio.CancelledError):
                    async with bot_api.user_interaction():
                        await bot_api.send_message("Hello!")

                # Verify that the queue cleanup was attempted before the raise
                mock_remove_queue.assert_called_once_with(mock_user_interaction)

    # Run the async test function
    asyncio.run(run_test())


# ---------------------------------------------------------------------------
# Durable outbound notification queue (producer side; delivery is tested in
# telegram_dispatcher_test.py)
# ---------------------------------------------------------------------------

OUTBOUND_USER_ID = 123
QUEUE = f"outbound-messages:{OUTBOUND_USER_ID}"


def test_notify_enqueues_and_bounds_backlog() -> None:
    fake = FakeRedis()
    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = TelegramBotApi(OUTBOUND_USER_ID)

        async def go() -> None:
            for i in range(OUTBOUND_QUEUE_MAX + 5):
                await bot.notify(f"m{i}")

        asyncio.run(go())

    # Backlog is capped, keeping the newest entries.
    assert len(fake.lists[QUEUE]) == OUTBOUND_QUEUE_MAX
    newest = json.loads(fake.lists[QUEUE][0])
    assert newest["text"] == f"m{OUTBOUND_QUEUE_MAX + 4}"
