import asyncio
import contextlib
import json
import pytest
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter, BadRequest

from spanreed.apis.telegram_bot import TelegramBotApi, OUTBOUND_QUEUE_MAX
from spanreed.user import User


@patch.object(TelegramBotApi, "get_application")
def test_user_interaction_simple_usage(
    mock_get_application: AsyncMock,
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

        # Verify that a message was sent
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
                mock_user_interaction_class.return_value = (
                    mock_user_interaction
                )
                # This simulates cancellation during the queue waiting phase
                mock_user_interaction.wait_to_run.side_effect = (
                    asyncio.CancelledError("Test cancellation")
                )

                # After the fix, this should raise CancelledError (not RuntimeError)
                # because the context manager now properly raises instead of returning
                with pytest.raises(asyncio.CancelledError):
                    async with bot_api.user_interaction():
                        await bot_api.send_message("Hello!")

                # Verify that the queue cleanup was attempted before the raise
                mock_remove_queue.assert_called_once_with(
                    mock_user_interaction
                )

    # Run the async test function
    asyncio.run(run_test())


# ---------------------------------------------------------------------------
# Durable outbound notification queue
# ---------------------------------------------------------------------------

OUTBOUND_USER_ID = 123
QUEUE = f"outbound-messages:{OUTBOUND_USER_ID}"
INFLIGHT = f"outbound-messages:inflight:{OUTBOUND_USER_ID}"


class FakeRedis:
    """In-memory stand-in for the Redis list ops the outbound queue uses.

    Lists are modeled head-first (index 0 == left/head), matching Redis:
    LPUSH inserts at the head, so the tail is the oldest element.
    """

    def __init__(self) -> None:
        self.lists: dict[str, list] = {}

    async def lpush(self, key: str, *values: object) -> int:
        lst = self.lists.setdefault(key, [])
        for value in values:
            lst.insert(0, value)
        return len(lst)

    async def rpush(self, key: str, *values: object) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    async def lpop(self, key: str) -> object | None:
        lst = self.lists.get(key, [])
        return lst.pop(0) if lst else None

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1]
        return True

    async def brpoplpush(
        self, src: str, dst: str, timeout: int = 0
    ) -> object | None:
        lst = self.lists.get(src, [])
        if not lst:
            return None
        value = lst.pop()  # tail == oldest
        self.lists.setdefault(dst, []).insert(0, value)
        return value

    async def lrem(self, key: str, count: int, value: object) -> int:
        lst = self.lists.get(key, [])
        removed = 0
        while value in lst and (count == 0 or removed < count):
            lst.remove(value)
            removed += 1
        return removed


class FakeRedisCancelOnEmpty(FakeRedis):
    """FakeRedis whose blocking pop raises CancelledError when the queue is

    empty, so the consumer loop terminates in a test once it has drained.
    """

    async def brpoplpush(
        self, src: str, dst: str, timeout: int = 0
    ) -> object | None:
        value = await super().brpoplpush(src, dst, timeout)
        if value is None:
            raise asyncio.CancelledError
        return value


def _outbound_bot(fake: FakeRedis) -> TelegramBotApi:
    bot = TelegramBotApi(OUTBOUND_USER_ID)
    bot.send_message = AsyncMock()  # type: ignore[method-assign]
    return bot


def test_notify_enqueues_and_bounds_backlog() -> None:
    fake = FakeRedis()
    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = _outbound_bot(fake)

        async def go() -> None:
            for i in range(OUTBOUND_QUEUE_MAX + 5):
                await bot.notify(f"m{i}")

        asyncio.run(go())

    # Backlog is capped, keeping the newest entries.
    assert len(fake.lists[QUEUE]) == OUTBOUND_QUEUE_MAX
    newest = json.loads(fake.lists[QUEUE][0])
    assert newest["text"] == f"m{OUTBOUND_QUEUE_MAX + 4}"


def test_deliver_one_success_acks() -> None:
    fake = FakeRedis()
    raw = json.dumps({"text": "hello", "parse_html": True})
    fake.lists[INFLIGHT] = [raw]
    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = _outbound_bot(fake)
        asyncio.run(bot._deliver_one(raw))

    bot.send_message.assert_awaited_once_with(
        "hello", parse_html=True, parse_markdown=False
    )
    assert fake.lists[INFLIGHT] == []  # acked


def test_deliver_one_retries_transient_then_acks() -> None:
    fake = FakeRedis()
    raw = json.dumps({"text": "hi"})
    fake.lists[INFLIGHT] = [raw]
    with (
        patch("spanreed.apis.telegram_bot.redis_api", new=fake),
        patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        bot = _outbound_bot(fake)
        bot.send_message = AsyncMock(  # type: ignore[method-assign]
            side_effect=[RetryAfter(2), None]
        )
        asyncio.run(bot._deliver_one(raw))

    assert bot.send_message.await_count == 2
    mock_sleep.assert_awaited()  # it backed off before retrying
    assert fake.lists[INFLIGHT] == []  # eventually acked


def test_deliver_one_drops_permanent_error() -> None:
    fake = FakeRedis()
    raw = json.dumps({"text": "bad markup"})
    fake.lists[INFLIGHT] = [raw]
    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = _outbound_bot(fake)
        bot.send_message = AsyncMock(  # type: ignore[method-assign]
            side_effect=BadRequest("nope")
        )
        asyncio.run(bot._deliver_one(raw))

    # Tried once, then dropped (removed from inflight) rather than retried.
    bot.send_message.assert_awaited_once()
    assert fake.lists[INFLIGHT] == []


def test_deliver_one_drops_malformed_entry() -> None:
    fake = FakeRedis()
    raw = "this is not json"
    fake.lists[INFLIGHT] = [raw]
    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = _outbound_bot(fake)
        asyncio.run(bot._deliver_one(raw))

    bot.send_message.assert_not_awaited()
    assert fake.lists[INFLIGHT] == []


def test_recover_inflight_returns_messages_to_queue_tail() -> None:
    fake = FakeRedis()
    fake.lists[INFLIGHT] = ["stranded"]
    fake.lists[QUEUE] = ["newer"]  # already queued (head == newest)
    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = _outbound_bot(fake)
        asyncio.run(bot._recover_inflight())

    assert fake.lists[INFLIGHT] == []
    # Recovered message goes to the tail (oldest), so it is delivered first.
    assert fake.lists[QUEUE][-1] == "stranded"


def test_consumer_drains_fifo_and_recovers_inflight() -> None:
    fake = FakeRedisCancelOnEmpty()
    # A message reserved but not acked before a crash.
    fake.lists[INFLIGHT] = [json.dumps({"text": "stranded"})]

    sent: list[str] = []

    def collect(text: str, **_kwargs: object) -> None:
        sent.append(text)

    with patch("spanreed.apis.telegram_bot.redis_api", new=fake):
        bot = _outbound_bot(fake)
        bot.send_message = AsyncMock(side_effect=collect)  # type: ignore[method-assign]

        async def go() -> None:
            await bot.notify("a")
            await bot.notify("b")
            with contextlib.suppress(asyncio.CancelledError):
                await bot.deliver_outbound_messages()

        asyncio.run(go())

    # Recovered message delivered first, then the queue in FIFO order.
    assert sent == ["stranded", "a", "b"]
    assert fake.lists.get(QUEUE, []) == []
    assert fake.lists.get(INFLIGHT, []) == []
