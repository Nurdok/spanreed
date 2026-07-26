import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import InvalidCallbackData

from spanreed.apis.telegram_bot import (
    CallbackData,
    TelegramBotApi,
    TelegramBotPlugin,
    OUTBOUND_QUEUE_MAX,
)
from spanreed.interaction import engine
from spanreed.interaction.engine import PromptKind
from spanreed.plugin import Plugin
from spanreed.test_utils import FakeRedis


def setup_function() -> None:
    engine.reset_state_for_tests()


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


def test_unknown_interaction_id_answered_as_expired() -> None:
    """A button press for a prompt from before a restart (engine state is
    in-memory) must be answered as expired, not crash or delete anything."""
    Plugin.reset_registry()
    plugin = TelegramBotPlugin()

    update = MagicMock()
    query = AsyncMock()
    query.data = CallbackData("no-such-interaction", 123, 0)
    update.callback_query = query
    context = MagicMock()

    asyncio.run(plugin.handle_callback_query(update, context))

    query.answer.assert_awaited_once()
    query.delete_message.assert_not_called()


def test_callback_query_resolves_engine_prompt() -> None:
    """A button press delivers the chosen index to the engine's pending
    prompt (and deletes the keyboard message via the dispatcher)."""
    Plugin.reset_registry()
    plugin = TelegramBotPlugin()

    async def run() -> None:
        prompt = engine.create_prompt(7, PromptKind.CHOICE)

        update = MagicMock()
        query = AsyncMock()
        query.data = CallbackData(prompt.interaction_id, 123, 2)
        update.callback_query = query
        context = MagicMock()

        with patch.object(TelegramBotApi, "_dispatch", new_callable=AsyncMock):
            await plugin.handle_callback_query(update, context)

        assert await prompt.future == 2

    asyncio.run(run())


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
