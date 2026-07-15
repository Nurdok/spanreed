import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiolimiter import AsyncLimiter
from telegram.error import BadRequest, RetryAfter, TimedOut

from spanreed.apis.telegram_dispatcher import OutboundDispatcher
from spanreed.test_utils import FakeRedis

CHAT_ID = 123
QUEUE = f"outbound-messages:{CHAT_ID}"
INFLIGHT = f"outbound-messages:inflight:{CHAT_ID}"


def make_dispatcher(
    fake: FakeRedis,
    send_text: AsyncMock | None = None,
    **kwargs: Any,
) -> OutboundDispatcher:
    kwargs.setdefault("per_chat_min_interval", 0.0)
    kwargs.setdefault("resume_jitter", (0.0, 0.0))
    kwargs.setdefault("idle_poll_interval", 0.01)
    kwargs.setdefault("global_limiter", AsyncLimiter(10_000, 1))
    return OutboundDispatcher(
        chat_id=CHAT_ID,
        redis=fake,
        send_text=send_text if send_text is not None else AsyncMock(),
        **kwargs,
    )


def test_high_job_result_propagates() -> None:
    async def go() -> None:
        dispatcher = make_dispatcher(FakeRedis())
        api_call = AsyncMock(return_value="the-message")
        caller = asyncio.create_task(dispatcher.enqueue("send", api_call))
        await asyncio.sleep(0)  # let the caller enqueue the job

        await dispatcher._process_next()

        assert await caller == "the-message"
        api_call.assert_awaited_once()

    asyncio.run(go())


def test_high_job_exception_propagates() -> None:
    async def go() -> None:
        dispatcher = make_dispatcher(FakeRedis())
        api_call = AsyncMock(side_effect=ValueError("boom"))
        caller = asyncio.create_task(dispatcher.enqueue("send", api_call))
        await asyncio.sleep(0)

        await dispatcher._process_next()

        with pytest.raises(ValueError):
            await caller

    asyncio.run(go())


def test_high_executes_before_low() -> None:
    async def go() -> None:
        fake = FakeRedis()
        order: list[str] = []

        async def send_text(text: str, *_args: Any) -> None:
            order.append(f"low:{text}")

        dispatcher = make_dispatcher(fake, send_text=AsyncMock(side_effect=send_text))
        await fake.lpush(QUEUE, json.dumps({"text": "notification"}))

        async def high_call() -> None:
            order.append("high")

        caller = asyncio.create_task(dispatcher.enqueue("send", high_call))
        await asyncio.sleep(0)

        await dispatcher._process_next()  # HIGH takes priority
        await dispatcher._process_next()  # then the LOW entry

        await caller
        assert order == ["high", "low:notification"]

    asyncio.run(go())


def test_pacing_spaces_out_sends() -> None:
    async def go() -> None:
        interval = 0.05
        dispatcher = make_dispatcher(FakeRedis(), per_chat_min_interval=interval)
        send_times: list[float] = []

        async def api_call() -> None:
            send_times.append(asyncio.get_running_loop().time())

        callers = [
            asyncio.create_task(dispatcher.enqueue("send", api_call)) for _ in range(2)
        ]
        await asyncio.sleep(0)

        await dispatcher._process_next()
        await dispatcher._process_next()
        for caller in callers:
            await caller

        assert len(send_times) == 2
        # Allow a little slack for event-loop timer resolution.
        assert send_times[1] - send_times[0] >= interval * 0.9

    asyncio.run(go())


def test_short_retry_after_retries_same_job() -> None:
    async def go() -> None:
        dispatcher = make_dispatcher(FakeRedis())
        api_call = AsyncMock(side_effect=[RetryAfter(0), "ok"])
        caller = asyncio.create_task(dispatcher.enqueue("send", api_call))
        await asyncio.sleep(0)

        await dispatcher._process_next()

        assert await caller == "ok"
        assert api_call.await_count == 2

    asyncio.run(go())


def test_long_retry_after_fails_high_fast() -> None:
    async def go() -> None:
        dispatcher = make_dispatcher(FakeRedis(), ban_fail_threshold=60)
        banned_call = AsyncMock(side_effect=RetryAfter(9999))
        queued_call = AsyncMock()
        caller1 = asyncio.create_task(dispatcher.enqueue("a", banned_call))
        await asyncio.sleep(0)
        caller2 = asyncio.create_task(dispatcher.enqueue("b", queued_call))
        await asyncio.sleep(0)

        await dispatcher._process_next()

        # The job that hit the ban and the one queued behind it both fail.
        with pytest.raises(RetryAfter):
            await caller1
        with pytest.raises(RetryAfter):
            await caller2
        queued_call.assert_not_awaited()

        # New jobs fail immediately while the ban pause is active.
        with pytest.raises(RetryAfter):
            await dispatcher.enqueue("c", AsyncMock())

    asyncio.run(go())


def test_low_delivered_and_acked() -> None:
    async def go() -> None:
        fake = FakeRedis()
        send_text = AsyncMock()
        dispatcher = make_dispatcher(fake, send_text=send_text)
        await fake.lpush(QUEUE, json.dumps({"text": "hello", "parse_html": True}))

        await dispatcher._process_next()

        send_text.assert_awaited_once_with("hello", True, False)
        assert fake.lists.get(QUEUE, []) == []
        assert fake.lists.get(INFLIGHT, []) == []

    asyncio.run(go())


def test_low_drains_fifo() -> None:
    async def go() -> None:
        fake = FakeRedis()
        sent: list[str] = []

        async def collect(text: str, *_args: Any) -> None:
            sent.append(text)

        dispatcher = make_dispatcher(fake, send_text=AsyncMock(side_effect=collect))
        await fake.lpush(QUEUE, json.dumps({"text": "a"}))
        await fake.lpush(QUEUE, json.dumps({"text": "b"}))

        await dispatcher._process_next()
        await dispatcher._process_next()

        assert sent == ["a", "b"]

    asyncio.run(go())


def test_low_permanent_error_dropped_and_acked() -> None:
    async def go() -> None:
        fake = FakeRedis()
        send_text = AsyncMock(side_effect=BadRequest("nope"))
        dispatcher = make_dispatcher(fake, send_text=send_text)
        await fake.lpush(QUEUE, json.dumps({"text": "bad markup"}))

        await dispatcher._process_next()

        send_text.assert_awaited_once()
        assert fake.lists.get(QUEUE, []) == []
        assert fake.lists.get(INFLIGHT, []) == []  # acked (dropped)

    asyncio.run(go())


def test_low_malformed_entry_dropped() -> None:
    async def go() -> None:
        fake = FakeRedis()
        send_text = AsyncMock()
        dispatcher = make_dispatcher(fake, send_text=send_text)
        await fake.lpush(QUEUE, "this is not json")

        await dispatcher._process_next()

        send_text.assert_not_awaited()
        assert fake.lists.get(INFLIGHT, []) == []

    asyncio.run(go())


def test_low_transient_error_keeps_entry_and_retries() -> None:
    async def go() -> None:
        fake = FakeRedis()
        raw = json.dumps({"text": "hi"})
        send_text = AsyncMock(side_effect=[TimedOut(), None])
        dispatcher = make_dispatcher(fake, send_text=send_text)
        await fake.lpush(QUEUE, raw)

        await dispatcher._process_next()

        # Still reserved (not acked) and the dispatcher backed off.
        assert fake.lists[INFLIGHT] == [raw]
        assert dispatcher._remaining_pause() > 0

        # Skip the backoff and retry: the same message is delivered.
        dispatcher._paused_until = None
        await dispatcher._process_next()

        assert send_text.await_count == 2
        assert fake.lists.get(INFLIGHT, []) == []

    asyncio.run(go())


def test_low_long_retry_after_keeps_entry_and_bans_high() -> None:
    async def go() -> None:
        fake = FakeRedis()
        raw = json.dumps({"text": "hi"})
        send_text = AsyncMock(side_effect=RetryAfter(9999))
        dispatcher = make_dispatcher(fake, send_text=send_text)
        await fake.lpush(QUEUE, raw)

        await dispatcher._process_next()

        # The durable entry survives the ban...
        assert fake.lists[INFLIGHT] == [raw]
        # ...but interactive sends fail fast while it lasts.
        with pytest.raises(RetryAfter):
            await dispatcher.enqueue("send", AsyncMock())

    asyncio.run(go())


def test_recover_inflight_returns_messages_to_queue_tail() -> None:
    async def go() -> None:
        fake = FakeRedis()
        fake.lists[INFLIGHT] = ["stranded"]
        fake.lists[QUEUE] = ["newer"]  # already queued (head == newest)
        dispatcher = make_dispatcher(fake)

        await dispatcher._recover_inflight()

        assert fake.lists[INFLIGHT] == []
        # Recovered message goes to the tail (oldest), so it is delivered first.
        assert fake.lists[QUEUE][-1] == "stranded"

    asyncio.run(go())
