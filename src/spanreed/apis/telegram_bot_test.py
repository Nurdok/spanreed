import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.user import User


@patch.object(TelegramBotApi, "get_application")
def test_user_interaction_simple_usage(
    mock_get_application: AsyncMock,
) -> None:
    """Test using the user_interaction context manager from the user's perspective."""

    async def run_test():
        # Setup mock application
        mock_app = AsyncMock()
        mock_app.bot_data = {}
        mock_get_application.return_value = mock_app

        # Create a bot API instance
        telegram_user_id = 123
        bot_api = TelegramBotApi(telegram_user_id)

        # Create a simple async function that uses user_interaction
        async def send_greeting():
            async with bot_api.user_interaction():
                await bot_api.send_message("Hello!")

        # Run the function
        await send_greeting()

        # Verify that a message was sent
        mock_app.bot.send_message.assert_called_once_with(
            chat_id=telegram_user_id, text="Hello!", parse_mode="HTML"
        )

    # Run the async test function
    asyncio.run(run_test())
