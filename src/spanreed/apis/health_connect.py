"""Ingress for Android Health Connect data.

Health Connect is an on-device store with no cloud API, so the data has to be
*pushed* to us. A bridge app on the phone (``health-connect-webhook``) reads
Health Connect in the background and POSTs new records as JSON to the
``/health-connect`` route served by the Web UI plugin.

The HTTP handler must answer in milliseconds (the bridge app only retries a
few times), so it does nothing but authenticate the request and push the raw
body onto a per-user Redis inbox. ``HealthConnectPlugin`` drains the inbox,
parses the payload and stores the records per local date in Redis
(``HealthConnectStore``), where later PRs compute daily metrics from them.

Payload shape (from the bridge app's ``docs/webhook.md``)::

    {
      "timestamp": "2026-05-09T14:00:00.123Z",
      "app_version": "1.2.3",
      "steps": [{"count": 8421, "start_time": "...", "end_time": "..."}],
      "heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}],
      "resting_heart_rate": [{"bpm": 58, "time": "..."}]
    }

Every per-type key is optional and only present when the batch holds at
least one record. Timestamps are ISO-8601 UTC instants.
"""

import datetime
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable

from spanreed.storage import redis_api
from spanreed.user import User


# Public URL the bridge app POSTs to (served by the Web UI plugin behind the
# reverse proxy, like the OAuth redirect routes).
WEBHOOK_URL = "https://spanreed.ink/health-connect"

# How many raw payloads an inbox may hold before the oldest are dropped.
# Bounds memory if the plugin isn't draining (e.g. user unregistered but the
# phone keeps posting). Normal operation holds 0-1 entries.
INBOX_MAX_LENGTH = 500

# How long per-day record hashes are kept. Metrics are recomputed from these
# on every batch, and the manual sync commands (later PRs) re-read them, so
# keep enough history to re-sync "the last week" with room to spare.
RECORD_TTL = datetime.timedelta(days=30)

# Inbox poll timeout; the drain loop wakes up this often even when idle so a
# cancellation is noticed promptly.
INBOX_POLL_TIMEOUT_S = 60

_logger = logging.getLogger("spanreed.apis.health_connect")


# --- Parsed records ----------------------------------------------------------


@dataclass(frozen=True)
class HeartRateSample:
    time: datetime.datetime  # timezone-aware
    bpm: int


@dataclass(frozen=True)
class StepsRecord:
    start_time: datetime.datetime  # timezone-aware
    end_time: datetime.datetime  # timezone-aware
    count: int


@dataclass
class HealthConnectPayload:
    heart_rate: list[HeartRateSample] = field(default_factory=list)
    resting_heart_rate: list[HeartRateSample] = field(default_factory=list)
    steps: list[StepsRecord] = field(default_factory=list)
    app_version: str | None = None
    # Per-type keys we received but don't parse (yet). Logged so new types
    # the app starts sending are visible without a crash.
    unknown_types: list[str] = field(default_factory=list)
    # Records that didn't match the expected shape and were skipped.
    skipped_records: int = 0

    def is_empty(self) -> bool:
        return not (self.heart_rate or self.resting_heart_rate or self.steps)


# Top-level keys that are metadata rather than record arrays.
_METADATA_KEYS = {"timestamp", "app_version"}
_PARSED_TYPES = {"heart_rate", "resting_heart_rate", "steps"}


def parse_instant(value: Any) -> datetime.datetime:
    """Parse an ISO-8601 instant (``java.time.Instant.toString()`` output).

    Always returns a timezone-aware datetime. Naive strings are assumed UTC.
    """
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO-8601 string, got {value!r}")
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def parse_payload(data: Any) -> HealthConnectPayload:
    """Parse a webhook JSON object into typed records.

    Lenient by design: a single malformed record is skipped (and counted in
    ``skipped_records``) rather than failing the whole batch, and unknown
    per-type keys are collected in ``unknown_types``. A payload that isn't a
    JSON object at all raises ``ValueError``.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Payload is not a JSON object: {type(data).__name__}")

    payload = HealthConnectPayload()
    app_version = data.get("app_version")
    payload.app_version = str(app_version) if app_version is not None else None

    for type_name in ("heart_rate", "resting_heart_rate"):
        records = data.get(type_name)
        if records is None:
            continue
        if not isinstance(records, list):
            _logger.warning(f"Ignoring non-list {type_name!r}: {records!r}")
            continue
        target = getattr(payload, type_name)
        for record in records:
            try:
                target.append(
                    HeartRateSample(
                        time=parse_instant(record["time"]),
                        bpm=int(record["bpm"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                _logger.warning(
                    f"Skipping malformed {type_name} record {record!r}: {e}"
                )
                payload.skipped_records += 1

    steps = data.get("steps")
    if steps is not None:
        if not isinstance(steps, list):
            _logger.warning(f"Ignoring non-list 'steps': {steps!r}")
        else:
            for record in steps:
                try:
                    payload.steps.append(
                        StepsRecord(
                            start_time=parse_instant(record["start_time"]),
                            end_time=parse_instant(record["end_time"]),
                            count=int(record["count"]),
                        )
                    )
                except (KeyError, TypeError, ValueError) as e:
                    _logger.warning(f"Skipping malformed steps record {record!r}: {e}")
                    payload.skipped_records += 1

    payload.unknown_types = sorted(
        key for key in data if key not in _METADATA_KEYS and key not in _PARSED_TYPES
    )
    return payload


def local_date(
    instant: datetime.datetime, tz: datetime.tzinfo | None = None
) -> datetime.date:
    """The calendar date of an instant in ``tz`` (server-local by default).

    Daily notes are keyed by local date, and the other daily-note plugins
    bucket on the server's local midnight too, so this keeps them consistent.
    """
    return instant.astimezone(tz).date()


# --- Webhook ingress (token registry + inbox) --------------------------------


class HealthConnectApi:
    """Static helpers shared by the HTTP route and the plugin."""

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _token_key(token: str) -> str:
        # Key by a digest so the secret itself never appears in key listings.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"health-connect:token:{digest}"

    @staticmethod
    def inbox_key(user_id: int) -> str:
        return f"health-connect:inbox:{user_id}"

    @classmethod
    async def register_token(cls, user: User, token: str) -> None:
        await redis_api.set(cls._token_key(token), str(user.id))

    @classmethod
    async def unregister_token(cls, token: str) -> None:
        await redis_api.delete(cls._token_key(token))

    @classmethod
    async def user_id_for_token(cls, token: str) -> int | None:
        if not token:
            return None
        raw = await redis_api.get(cls._token_key(token))
        if raw is None:
            return None
        return int(raw)

    @classmethod
    async def enqueue_payload(cls, user_id: int, body: str) -> None:
        key = cls.inbox_key(user_id)
        await redis_api.lpush(key, body)
        await redis_api.ltrim(key, 0, INBOX_MAX_LENGTH - 1)

    @classmethod
    async def dequeue_payload(cls, user: User) -> str | None:
        """Block for up to ``INBOX_POLL_TIMEOUT_S`` for the next raw payload.

        Returns the oldest queued body, or ``None`` on timeout.
        """
        result = await redis_api.brpop(
            cls.inbox_key(user.id), timeout=INBOX_POLL_TIMEOUT_S
        )
        if result is None:
            return None
        _, raw = result
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    @classmethod
    async def handle_webhook(cls, token: str, body: str) -> int:
        """Authenticate and enqueue a webhook delivery. Returns an HTTP status.

        Kept free of any slow work: the bridge app gives up after three
        attempts, and draining to Obsidian can take minutes when the desktop
        is offline.
        """
        user_id = await cls.user_id_for_token(token)
        if user_id is None:
            _logger.warning("Rejected Health Connect webhook: unknown token.")
            return 401
        try:
            data = json.loads(body)
        except ValueError:
            _logger.warning(
                f"Rejected Health Connect webhook for user {user_id}: bad JSON"
            )
            return 400
        if not isinstance(data, dict):
            return 400
        await cls.enqueue_payload(user_id, body)
        _logger.info(
            f"Queued Health Connect payload for user {user_id} "
            f"({len(body)} bytes, types={sorted(data.keys() - _METADATA_KEYS)})"
        )
        return 202


# --- Per-day record store ----------------------------------------------------


# Field/value types redis-py accepts for HSET's ``mapping``.
_HashMapping = dict[str | bytes, bytes | float | int | str]


def _epoch_ms(instant: datetime.datetime) -> int:
    return int(instant.timestamp() * 1000)


def _from_epoch_ms(value: str | bytes) -> datetime.datetime:
    ms = int(value)
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)


def _decode(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class HealthConnectStore:
    """Per-user, per-local-date record buffer in Redis.

    The bridge app delivers records incrementally (a watermark per type over a
    48h window), so a day's records arrive across many batches, and updated
    records are re-sent. Each day is a Redis hash keyed by the record's
    timestamp, so re-deliveries overwrite in place and the full day can be
    re-read to compute metrics.
    """

    def __init__(self, user: User, tz: datetime.tzinfo | None = None) -> None:
        self._user = user
        self._tz = tz

    def _key(self, record_type: str, date: datetime.date) -> str:
        return f"health-connect:{self._user.id}:{record_type}:{date.isoformat()}"

    async def _write(
        self,
        record_type: str,
        by_date: dict[datetime.date, _HashMapping],
    ) -> set[datetime.date]:
        for date, mapping in by_date.items():
            if not mapping:
                continue
            key = self._key(record_type, date)
            await redis_api.hset(key, mapping=mapping)
            await redis_api.expire(key, int(RECORD_TTL.total_seconds()))
        return {date for date, mapping in by_date.items() if mapping}

    async def add_heart_rate(
        self, samples: Iterable[HeartRateSample], record_type: str = "heart-rate"
    ) -> set[datetime.date]:
        """Store samples; returns the set of local dates that were touched."""
        by_date: dict[datetime.date, _HashMapping] = {}
        for sample in samples:
            by_date.setdefault(local_date(sample.time, self._tz), {})[
                str(_epoch_ms(sample.time))
            ] = str(sample.bpm)
        return await self._write(record_type, by_date)

    async def add_resting_heart_rate(
        self, samples: Iterable[HeartRateSample]
    ) -> set[datetime.date]:
        return await self.add_heart_rate(samples, record_type="resting-heart-rate")

    async def add_steps(self, records: Iterable[StepsRecord]) -> set[datetime.date]:
        """Store step intervals, bucketed by the local date they start on."""
        by_date: dict[datetime.date, _HashMapping] = {}
        for record in records:
            by_date.setdefault(local_date(record.start_time, self._tz), {})[
                str(_epoch_ms(record.start_time))
            ] = f"{_epoch_ms(record.end_time)}:{record.count}"
        return await self._write("steps", by_date)

    async def get_heart_rate(
        self, date: datetime.date, record_type: str = "heart-rate"
    ) -> list[HeartRateSample]:
        raw: dict = await redis_api.hgetall(self._key(record_type, date))
        samples = [
            HeartRateSample(time=_from_epoch_ms(_decode(k)), bpm=int(_decode(v)))
            for k, v in raw.items()
        ]
        return sorted(samples, key=lambda s: s.time)

    async def get_resting_heart_rate(
        self, date: datetime.date
    ) -> list[HeartRateSample]:
        return await self.get_heart_rate(date, record_type="resting-heart-rate")

    async def get_steps(self, date: datetime.date) -> list[StepsRecord]:
        raw: dict = await redis_api.hgetall(self._key("steps", date))
        records = []
        for k, v in raw.items():
            end_ms, count = _decode(v).split(":")
            records.append(
                StepsRecord(
                    start_time=_from_epoch_ms(_decode(k)),
                    end_time=_from_epoch_ms(end_ms),
                    count=int(count),
                )
            )
        return sorted(records, key=lambda r: r.start_time)
