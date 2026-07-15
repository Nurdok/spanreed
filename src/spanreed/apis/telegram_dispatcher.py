"""Per-user outbound send dispatcher for the Telegram bot.

Every Telegram API call for a given chat goes through one
:class:`OutboundDispatcher`, which paces sends to Telegram's limits and
centralizes ``RetryAfter`` (flood control) handling. This is what prevents
the self-perpetuating multi-hour bans we used to get: during a ban the
dispatcher stops hitting the API entirely (interactive sends fail fast,
durable notifications wait in Redis), and it resumes with a small random
jitter so ban expiry never triggers a synchronized burst.

Two priority lanes:

- **HIGH** — an in-memory queue of :class:`SendJob`s: interactive sends,
  keyboard prompts, edits, deletes, documents. The caller awaits the job's
  future and gets the API result (e.g. the ``Message``) back.
- **LOW** — the durable per-user Redis notification queue produced by
  ``TelegramBotApi.notify()``. Entries are reserved into an inflight list
  before sending (crash-safe) and acked after, preserving the previous
  consumer's semantics: transient errors retry the same message with
  backoff, permanent errors drop it so a poison message can't wedge the
  queue.
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiolimiter import AsyncLimiter

from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TimedOut,
)

PER_CHAT_MIN_INTERVAL_S = 1.1
BAN_FAIL_THRESHOLD_S = 60.0
RESUME_JITTER_RANGE_S = (1.0, 10.0)
IDLE_POLL_INTERVAL_S = 2.0
LOW_INITIAL_BACKOFF_S = 60.0
LOW_MAX_BACKOFF_S = 30.0 * 60

# Shared across all dispatchers: Telegram's overall bot limit is ~30
# messages/second; stay under it.
_GLOBAL_LIMITER = AsyncLimiter(25, 1)


@dataclass
class SendJob:
    description: str
    run: Callable[[], Awaitable[Any]]
    future: asyncio.Future


class OutboundDispatcher:
    """Paced, prioritized executor for all sends to a single chat."""

    def __init__(
        self,
        chat_id: int,
        redis: Any,
        send_text: Callable[[str, bool, bool], Awaitable[Any]],
        *,
        per_chat_min_interval: float = PER_CHAT_MIN_INTERVAL_S,
        global_limiter: AsyncLimiter | None = None,
        ban_fail_threshold: float = BAN_FAIL_THRESHOLD_S,
        resume_jitter: tuple[float, float] = RESUME_JITTER_RANGE_S,
        idle_poll_interval: float = IDLE_POLL_INTERVAL_S,
    ) -> None:
        self._chat_id = chat_id
        self._redis = redis
        # Sends a plain notification text: (text, parse_html, parse_markdown).
        # Injected so this module stays independent of the bot wiring.
        self._send_text = send_text
        self._per_chat_min_interval = per_chat_min_interval
        self._global_limiter = (
            global_limiter if global_limiter is not None else _GLOBAL_LIMITER
        )
        self._ban_fail_threshold = ban_fail_threshold
        self._resume_jitter = resume_jitter
        self._idle_poll_interval = idle_poll_interval

        self._logger = logging.getLogger(f"{__name__}.OutboundDispatcher.{chat_id}")
        self._high: asyncio.Queue[SendJob] = asyncio.Queue()
        # A LOW entry reserved from Redis but not yet delivered/acked.
        self._pending_low: bytes | str | None = None
        self._low_backoff = LOW_INITIAL_BACKOFF_S
        self._last_send_at: float | None = None
        self._paused_until: float | None = None
        # While banned (a RetryAfter longer than the threshold), HIGH jobs
        # fail fast instead of queueing so interactive callers don't hang for
        # hours holding the user-interaction lock.
        self._banned = False

    def _outbound_key(self) -> str:
        return f"outbound-messages:{self._chat_id}"

    def _inflight_key(self) -> str:
        return f"outbound-messages:inflight:{self._chat_id}"

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def _remaining_pause(self) -> float:
        if self._paused_until is None:
            return 0.0
        return max(0.0, self._paused_until - self._now())

    async def enqueue(self, description: str, run: Callable[[], Awaitable[Any]]) -> Any:
        """Submit a HIGH-priority job and wait for its result.

        Raises ``RetryAfter`` immediately when the dispatcher is in a ban
        pause, with the remaining pause time.
        """
        if self._banned and self._remaining_pause() > 0:
            raise RetryAfter(int(self._remaining_pause()) + 1)
        job = SendJob(
            description=description,
            run=run,
            future=asyncio.get_running_loop().create_future(),
        )
        await self._high.put(job)
        return await job.future

    async def run(self) -> None:
        """Process jobs forever. Started once per user as a background task."""
        await self._recover_inflight()
        while True:
            try:
                await self._process_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Defensive: never let the dispatcher die (e.g. a Redis
                # blip); everything for this chat depends on it.
                self._logger.exception("Dispatcher loop error; retrying in 5s.")
                await asyncio.sleep(5)

    async def _recover_inflight(self) -> None:
        """Return messages reserved but not acked before a crash to the queue.

        Pushes them back on the right (oldest) end so they keep FIFO priority.
        """
        while (item := await self._redis.lpop(self._inflight_key())) is not None:
            await self._redis.rpush(self._outbound_key(), item)

    async def _process_next(self) -> None:
        """Process a single job (or idle briefly when there's nothing to do)."""
        await self._wait_out_pause()

        if not self._high.empty():
            await self._execute_high(self._high.get_nowait())
            return

        if self._pending_low is None:
            self._pending_low = await self._redis.rpoplpush(
                self._outbound_key(), self._inflight_key()
            )
        if self._pending_low is not None:
            await self._deliver_low(self._pending_low)
            return

        # Nothing to do: block on the HIGH queue briefly, then loop so we
        # also notice new LOW entries in Redis.
        try:
            job = await asyncio.wait_for(
                self._high.get(), timeout=self._idle_poll_interval
            )
        except asyncio.TimeoutError:
            return
        await self._wait_out_pause()
        await self._execute_high(job)

    async def _wait_out_pause(self) -> None:
        remaining = self._remaining_pause()
        if remaining > 0:
            self._logger.info(f"Paused for another {remaining:.0f}s.")
            await asyncio.sleep(remaining)
        self._paused_until = None
        self._banned = False

    async def _pace(self) -> None:
        if self._last_send_at is not None:
            wait = self._per_chat_min_interval - (self._now() - self._last_send_at)
            if wait > 0:
                await asyncio.sleep(wait)
        async with self._global_limiter:
            self._last_send_at = self._now()

    def _enter_pause(self, retry_after: float) -> None:
        jitter = random.uniform(*self._resume_jitter)
        self._paused_until = self._now() + retry_after + jitter
        self._logger.warning(
            f"Flood control: pausing sends for {retry_after:.0f}s (+{jitter:.1f}s jitter)."
        )
        if retry_after > self._ban_fail_threshold:
            self._banned = True
            self._fail_queued_high(retry_after)

    def _fail_queued_high(self, retry_after: float) -> None:
        """Fail all queued HIGH jobs so interactive callers don't hang."""
        failed = 0
        while not self._high.empty():
            job = self._high.get_nowait()
            if not job.future.done():
                job.future.set_exception(RetryAfter(int(retry_after)))
            failed += 1
        if failed:
            self._logger.warning(
                f"Failed {failed} queued interactive send(s) due to flood-control ban."
            )

    async def _execute_high(self, job: SendJob) -> None:
        if job.future.done():  # e.g. failed while banned, or caller gone
            return
        while True:
            await self._pace()
            try:
                result = await job.run()
            except RetryAfter as e:
                self._enter_pause(e.retry_after)
                if self._banned:
                    if not job.future.done():
                        job.future.set_exception(e)
                    return
                # Short throttle: wait it out and retry the same job.
                await self._wait_out_pause()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                job.future.set_exception(e)
                return
            job.future.set_result(result)
            return

    async def _deliver_low(self, raw: bytes | str) -> None:
        """Deliver one reserved LOW entry; ack on success or permanent drop."""
        try:
            message = json.loads(raw)
            text = message["text"]
            parse_html = message.get("parse_html", True)
            parse_markdown = message.get("parse_markdown", False)
        except (ValueError, KeyError, TypeError):
            self._logger.exception(f"Dropping malformed outbound entry: {raw!r}")
            await self._ack_low(raw)
            return

        try:
            await self._pace()
            await self._send_text(text, parse_html, parse_markdown)
        except asyncio.CancelledError:
            raise
        except RetryAfter as e:
            # Keep the entry reserved; the pause delays the next attempt.
            self._enter_pause(e.retry_after)
            return
        except (BadRequest, Forbidden):
            # Permanent (bad markup, message too long, bot blocked):
            # retrying can't help, so drop it. Caught before the transient
            # branch because BadRequest is a subclass of NetworkError.
            self._logger.exception(f"Dropping undeliverable outbound message: {text!r}")
        except (TimedOut, NetworkError, TimeoutError) as e:
            # Transient: Telegram is unreachable. Keep the entry reserved and
            # back off exponentially before the next attempt.
            self._logger.warning(
                f"Outbound delivery deferred ({e!r}); retrying in {self._low_backoff:.0f}s."
            )
            self._paused_until = self._now() + self._low_backoff
            self._low_backoff = min(self._low_backoff * 2, LOW_MAX_BACKOFF_S)
            return
        except Exception:
            # Any other error (unexpected / non-Telegram): drop rather than
            # wedge the queue.
            self._logger.exception(f"Dropping undeliverable outbound message: {text!r}")
        # Delivered or permanently dropped.
        self._low_backoff = LOW_INITIAL_BACKOFF_S
        await self._ack_low(raw)

    async def _ack_low(self, raw: bytes | str) -> None:
        await self._redis.lrem(self._inflight_key(), 1, raw)
        self._pending_low = None
