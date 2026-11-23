"""Receive Android Health Connect data.

Health Connect is a store on the phone. It has no cloud API. A bridge app on
the phone (``health-connect-webhook``) reads Health Connect in the background
and POSTs new records as JSON to the ``/health-connect`` route of the Web UI
plugin.

The HTTP handler must answer fast, because the bridge app retries only a few
times. It only checks the token and pushes the raw body onto a Redis inbox
for the user. ``HealthConnectPlugin`` reads the inbox, parses each payload
and stores every record by local date in ``HealthConnectStore``, exactly as
the phone sent it. Daily metrics and the raw data files in the vault are
both built from that store.

The bridge app sends this payload shape (see its ``docs/webhook.md``)::

    {
      "timestamp": "2026-05-09T14:00:00.123Z",
      "app_version": "1.2.3",
      "timezone": "UTC",
      "steps": [{"count": 8421, "start_time": "...", "end_time": "...",
                 "metadata": {"id": "0a1b...", "data_origin": "..."}}],
      "heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z",
                      "metadata": {"id": "2c3d..."}}],
      "resting_heart_rate": [{"bpm": 58, "time": "..."}],
      "vo2_max": [{"ml_per_kg_per_min": 45.3, "time": "..."}],
      "deleted": [{"type": "steps", "id": "4e5f..."}]
    }

``timestamp``, ``app_version`` and ``timezone`` are metadata. ``timezone`` is
a string that names the zone the app writes the payload times in: ``UTC`` or
the zone of the phone.

Each record-type key is optional. It is present only when the batch has at
least one record of that type. Timestamps are ISO-8601 instants. The app
can send more record types than the ones shown. Those are stored too, but
no metric is computed from them. A record carries a ``metadata`` object,
and ``metadata.id`` is the Health Connect id of the record. Older versions
of the app did not send it.

``deleted`` is optional too. It lists the records that were deleted on the
phone since the last sync. ``type`` is the record-type key and ``id`` is the
``metadata.id`` of a record sent before. A payload can hold only deletions.

The keys and their shapes were read from the app's ``docs/webhook.md`` and
``SyncManager.kt``. They were not checked against a payload from a phone.
"""

import datetime
import hashlib
import json
import logging
import re
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

# A record-type key of the payload that is safe to use in a Redis key and in
# a vault path.
_TYPE_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
# Prefix of the hash field of a record that has no usable timestamp field
# name; the rest is a digest of the record.
_DIGEST_FIELD_PREFIX = "h:"

# Daily note properties for the minutes in each heart rate zone, and the
# share of the maximum heart rate at which each zone starts.
HR_MODERATE_MINUTES = "hr-moderate-minutes"
HR_VIGOROUS_MINUTES = "hr-vigorous-minutes"
HR_MODERATE_FROM = 0.64
HR_VIGOROUS_FROM = 0.77

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
_METADATA_KEYS = {"timestamp", "app_version", "timezone"}
# Top-level key that lists the records deleted on the phone. It is not a
# health record type, so it is left out of the record types that are logged.
DELETED_KEY = "deleted"
# Payload-style name under which the store keeps each record that a deletion
# removed, so that no data the phone sent is lost.
DELETED_RECORDS_TYPE_NAME = "deleted_records"
# Top-level keys that are not health record types.
_NON_RECORD_KEYS = _METADATA_KEYS | {DELETED_KEY}
_PARSED_TYPES = {"heart_rate", "resting_heart_rate", "steps"}
# Record types that a daily metric is computed from. The first three are
# parsed into typed records; ``vo2_max`` is read back from the stored records.
_METRIC_TYPES = _PARSED_TYPES | {"vo2_max"}
# Record type of the store that holds the VO2 max measurements.
VO2_MAX_RECORD_TYPE = "vo2-max"


def parse_instant(value: Any) -> datetime.datetime:
    """Parse an ISO-8601 instant, as written by ``java.time.Instant``.

    Return a timezone-aware datetime. Treat a string with no timezone as
    UTC. Raise ``ValueError`` if the value is not a valid instant string.
    """
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO-8601 string, got {value!r}")
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _heart_rate_sample(record: Any) -> HeartRateSample:
    """Build a sample from one heart rate record of the payload.

    Raise ``KeyError``, ``TypeError`` or ``ValueError`` if the record does
    not have the expected shape.
    """
    return HeartRateSample(time=parse_instant(record["time"]), bpm=int(record["bpm"]))


def _steps_record(record: Any) -> StepsRecord:
    """Build a steps interval from one steps record of the payload.

    Raise ``KeyError``, ``TypeError`` or ``ValueError`` if the record does
    not have the expected shape.
    """
    return StepsRecord(
        start_time=parse_instant(record["start_time"]),
        end_time=parse_instant(record["end_time"]),
        count=int(record["count"]),
    )


def parse_payload(data: Any) -> HealthConnectPayload:
    """Parse a webhook JSON object into typed records.

    Skip a record that has the wrong shape and count it in
    ``skipped_records``. Do not fail the whole batch for one bad record.
    Collect record-type keys that this parser does not know in
    ``unknown_types``. Raise ``ValueError`` if ``data`` is not a JSON object.
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
                target.append(_heart_rate_sample(record))
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
                    payload.steps.append(_steps_record(record))
                except (KeyError, TypeError, ValueError) as e:
                    _logger.warning(f"Skipping malformed steps record {record!r}: {e}")
                    payload.skipped_records += 1

    payload.unknown_types = [
        key for key in record_type_keys(data) if key not in _METRIC_TYPES
    ]
    return payload


def record_type_keys(data: dict[str, Any]) -> list[str]:
    """Return the sorted keys of a payload that name health record types.

    Leave out the metadata keys and ``deleted``.
    """
    return sorted(key for key in data if key not in _NON_RECORD_KEYS)


def parse_deletions(data: dict[str, Any]) -> dict[str, set[str]]:
    """Return the record ids that a payload reports as deleted, by type.

    Read the ``deleted`` list of the payload. Each key of the result is a
    record-type key of the payload. Skip an entry that is not an object
    with a string ``type`` and a non-empty string ``id``.
    """
    entries = data.get(DELETED_KEY)
    deletions: dict[str, set[str]] = {}
    if not isinstance(entries, list):
        return deletions
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        type_name, record_id = entry.get("type"), entry.get("id")
        if isinstance(type_name, str) and isinstance(record_id, str) and record_id:
            deletions.setdefault(type_name, set()).add(record_id)
    return deletions


def local_date(
    instant: datetime.datetime, tz: datetime.tzinfo | None = None
) -> datetime.date:
    """Return the calendar date of an instant in timezone ``tz``.

    Use the server's local timezone if ``tz`` is ``None``. Daily notes are
    named by the user's local date, so callers pass the user's timezone.
    """
    return instant.astimezone(tz).date()


# --- Daily metrics -----------------------------------------------------------


def daily_vo2_max(records: Iterable[Any]) -> float | None:
    """Return the last VO2 max measurement among ``records``.

    The caller passes the stored ``vo2_max`` records of one local date, as
    the JSON the phone sent: ``{"ml_per_kg_per_min": 45.3, "time": "..."}``.
    Return the value in mL/(kg·min), rounded to one decimal place. Skip a
    record that does not have this shape. Return ``None`` if no record is
    left.
    """
    latest: tuple[datetime.datetime, float] | None = None
    for record in records:
        try:
            time = parse_instant(record["time"])
            value = record["ml_per_kg_per_min"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if latest is None or time >= latest[0]:
            latest = (time, float(value))
    return None if latest is None else round(latest[1], 1)


def daily_step_total(records: Iterable[StepsRecord]) -> int:
    """Return the sum of the step counts in ``records``.

    The caller passes the records of one local date. Health Connect gives
    one interval per source and period, so the sum is the day's total.
    """
    return sum(record.count for record in records)


def percentile(values: list[int], pct: float) -> float:
    """Return the ``pct`` percentile (0-100) of ``values``.

    Interpolate linearly between the two nearest ranks, like NumPy's
    default. Raise ``ValueError`` if ``values`` is empty.
    """
    if not values:
        raise ValueError("percentile of an empty list")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def max_heart_rate(age: float) -> float:
    """Estimate the maximum heart rate at an age in years, in bpm.

    Use the formula of Tanaka et al.: 208 − 0.7 × age.
    """
    return 208 - 0.7 * age


def heart_rate_zone_minutes(
    heart_rate: Iterable[HeartRateSample], max_hr: float
) -> dict[str, int]:
    """Count the minutes of moderate and of vigorous heart rate.

    Return ``hr-moderate-minutes`` and ``hr-vigorous-minutes``. A clock
    minute counts once, by the average of its samples, so a minute with many
    samples weighs the same as a minute with one. A minute with no samples
    is not counted. Moderate is from 64% of ``max_hr`` up to, but not
    including, 77%. Vigorous is 77% and above.
    """
    by_minute: dict[int, list[int]] = {}
    for sample in heart_rate:
        by_minute.setdefault(int(sample.time.timestamp() // 60), []).append(sample.bpm)
    minutes = {HR_MODERATE_MINUTES: 0, HR_VIGOROUS_MINUTES: 0}
    for bpm in by_minute.values():
        average = sum(bpm) / len(bpm)
        if average >= HR_VIGOROUS_FROM * max_hr:
            minutes[HR_VIGOROUS_MINUTES] += 1
        elif average >= HR_MODERATE_FROM * max_hr:
            minutes[HR_MODERATE_MINUTES] += 1
    return minutes


def daily_heart_rate_metrics(
    heart_rate: Iterable[HeartRateSample],
    resting_heart_rate: Iterable[HeartRateSample],
    max_hr: float | None = None,
) -> dict[str, int]:
    """Summarize one local date of heart rate as whole numbers.

    Return ``heart-rate-p5``, ``heart-rate-avg``, ``heart-rate-p95``,
    ``heart-rate-p98`` and ``heart-rate-p99`` from the heart rate samples,
    and ``resting-heart-rate`` from the last resting heart rate record of
    the day. If ``max_hr`` is given, also return the keys of
    ``heart_rate_zone_minutes``. The keys are the daily note property names.
    Leave out a key when the day has no data for it. For the bpm values,
    every sample counts once, so periods with denser samples (such as a
    workout) weigh more.
    """
    samples = list(heart_rate)
    metrics: dict[str, int] = {}
    bpm = [sample.bpm for sample in samples]
    if bpm:
        metrics["heart-rate-p5"] = round(percentile(bpm, 5))
        metrics["heart-rate-avg"] = round(sum(bpm) / len(bpm))
        metrics["heart-rate-p95"] = round(percentile(bpm, 95))
        metrics["heart-rate-p98"] = round(percentile(bpm, 98))
        metrics["heart-rate-p99"] = round(percentile(bpm, 99))
        if max_hr is not None:
            metrics.update(heart_rate_zone_minutes(samples, max_hr))
    resting = sorted(resting_heart_rate, key=lambda sample: sample.time)
    if resting:
        metrics["resting-heart-rate"] = resting[-1].bpm
    return metrics


# --- Webhook ingress (token registry + inbox) --------------------------------


class HealthConnectApi:
    """Helpers that the HTTP route and the plugin share."""

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def token_from_header(auth: str) -> str:
        """Extract the token from an ``Authorization`` header value.

        Accept the scheme in any case and tolerate stray whitespace or
        backticks around the token, which a copy from a chat message can add.
        """
        scheme, _, rest = auth.strip().partition(" ")
        if scheme.lower() != "bearer":
            return ""
        return rest.strip().strip("`").strip()

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
        """Wait for the next raw payload in the user's inbox.

        Return the oldest queued body. Return ``None`` if no payload arrives
        within ``INBOX_POLL_TIMEOUT_S`` seconds.
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
        """Check the token, queue the delivery and return an HTTP status.

        Do no slow work here. The bridge app stops after three attempts, and
        a write to Obsidian can take minutes when the desktop is offline.
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
            f"({len(body)} bytes, types={record_type_keys(data)}, "
            f"deletions={sum(map(len, parse_deletions(data).values()))})"
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


def _format_instant(instant: datetime.datetime) -> str:
    """Format an instant the way the bridge app writes one."""
    utc = instant.astimezone(datetime.timezone.utc)
    timespec = "milliseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def dump_record(record: Any) -> str:
    """Serialize one record compactly, keeping its key order."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False)


def _digest_field(record: Any) -> str:
    """Return a hash field name that is the same for equal records."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_DIGEST_FIELD_PREFIX}{digest}"


def _record_instant(record: Any) -> datetime.datetime | None:
    """Return the instant a record of any type belongs to, if it has one.

    Prefer ``time``, then ``start_time``, then any other key that ends in
    ``time`` and holds an instant. Return ``None`` if there is none.
    """
    if not isinstance(record, dict):
        return None
    others = sorted(
        key
        for key in record
        if isinstance(key, str)
        and key.endswith("time")
        and key not in ("time", "start_time")
    )
    for key in ["time", "start_time", *others]:
        try:
            return parse_instant(record.get(key))
        except ValueError:
            continue
    return None


def _known_record_start(type_name: str, record: Any) -> datetime.datetime | None:
    """Return the start instant of a well-formed record of a parsed type.

    Return ``None`` for a record type that is not parsed, and for a record
    that does not have the expected shape.
    """
    try:
        if type_name in ("heart_rate", "resting_heart_rate"):
            return _heart_rate_sample(record).time
        if type_name == "steps":
            return _steps_record(record).start_time
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _record_id(record: Any) -> str | None:
    """Return the Health Connect id of a record, if the record carries one.

    The bridge app writes it as ``metadata.id``.
    """
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    record_id = metadata.get("id")
    return record_id if isinstance(record_id, str) and record_id else None


def _received_instant(data: dict[str, Any]) -> datetime.datetime | None:
    """Return the ``timestamp`` of a payload, or ``None`` if it has none."""
    try:
        return parse_instant(data.get("timestamp"))
    except ValueError:
        return None


@dataclass
class AppliedDeletions:
    """What the deletions of one payload changed in the store."""

    # Number of stored records that were removed.
    removed_records: int = 0
    # Number of deleted ids that matched no stored record.
    unmatched_ids: int = 0
    # Changed local dates for each record type of the store. This includes
    # the record type that keeps the removed records.
    touched: dict[str, set[datetime.date]] = field(default_factory=dict)
    # Record type and local date of each day that has no records left.
    emptied: set[tuple[str, datetime.date]] = field(default_factory=set)


def record_type_of(type_name: str) -> str:
    """Return the store's record type for a record-type key of the payload."""
    return type_name.replace("_", "-")


def type_name_of(record_type: str) -> str:
    """Return the payload's record-type key for a record type of the store."""
    return record_type.replace("-", "_")


class HealthConnectStore:
    """Store records in Redis, by user, record type and local date.

    The bridge app sends records in small batches. It sends only records that
    are new or changed since the last sync, over a 48-hour window. So the
    records of one day arrive in many batches, and a changed record is sent
    again. Each day of each record type is one Redis hash, and each value is
    one record as JSON, exactly as the phone sent it. The full day can be
    read back to compute metrics and to write the raw data file.

    A well-formed heart rate, resting heart rate or steps record has its
    timestamp in epoch milliseconds as the field name, so a record that is
    sent again replaces the old one. Any other record has a digest of its
    content as the field name. Sending it again unchanged stores nothing new,
    but a record that the phone corrects is stored next to the old one.

    Hashes written before records were stored as JSON hold only the bpm, or
    ``{end epoch ms}:{count}`` for steps. Those values are still read.
    """

    def __init__(self, user: User, tz: datetime.tzinfo | None = None) -> None:
        """Create a store for ``user``.

        Bucket records by local date in ``tz``. Use the user's own timezone
        if ``tz`` is ``None``.
        """
        self._user = user
        self._tz = tz if tz is not None else user.tzinfo

    @property
    def _prefix(self) -> str:
        return f"health-connect:{self._user.id}:"

    def _key(self, record_type: str, date: datetime.date) -> str:
        return f"{self._prefix}{record_type}:{date.isoformat()}"

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

    async def add_records(
        self,
        type_name: str,
        records: Iterable[Any],
        received: datetime.datetime | None = None,
    ) -> set[datetime.date]:
        """Store records of one payload key and return the changed dates.

        Accept any JSON value as a record. A record belongs to the local
        date of its timestamp. A record with no timestamp belongs to the
        local date of ``received``, which defaults to now.
        """
        if received is None:
            received = datetime.datetime.now(tz=datetime.timezone.utc)
        by_date: dict[datetime.date, _HashMapping] = {}
        for record in records:
            start = _known_record_start(type_name, record)
            if start is not None:
                field, instant = str(_epoch_ms(start)), start
            else:
                field = _digest_field(record)
                instant = _record_instant(record) or received
            by_date.setdefault(local_date(instant, self._tz), {})[field] = dump_record(
                record
            )
        return await self._write(record_type_of(type_name), by_date)

    async def add_payload(self, data: dict[str, Any]) -> dict[str, set[datetime.date]]:
        """Store every record of a webhook payload.

        Keep records of every type, with every field they carry. Treat a
        value that is not a list as one record. Return the changed local
        dates for each record type of the store.
        """
        received = _received_instant(data)
        touched: dict[str, set[datetime.date]] = {}
        for type_name, value in data.items():
            if type_name in _METADATA_KEYS:
                continue
            # ``deleted`` is not a health record type, but it is stored like
            # one, as raw data: the owner wants everything the phone sends
            # kept. ``apply_deletions`` acts on it.
            if not _TYPE_NAME_RE.fullmatch(type_name):
                _logger.warning(f"Not storing record type {type_name!r}: bad name.")
                continue
            records = value if isinstance(value, list) else [value]
            dates = await self.add_records(type_name, records, received)
            if dates:
                touched[record_type_of(type_name)] = dates
        return touched

    async def apply_deletions(self, data: dict[str, Any]) -> AppliedDeletions:
        """Remove the stored records that a payload reports as deleted.

        Match the ``id`` of each entry of ``deleted`` to the ``metadata.id``
        of the stored records of that type, on every stored date. Remove
        every match, because one Health Connect record can be sent as many
        records, and an edited record can be stored twice. Before a record
        is removed, store it under ``DELETED_RECORDS_TYPE_NAME`` as
        ``{"type": ..., "record": ...}``, on the local date of the payload,
        so that no data is lost.

        An id can match nothing: the record may never have been sent, may
        have expired, or may come from a version of the app that sent no
        ids. Count it in ``unmatched_ids`` and do nothing else.
        """
        result = AppliedDeletions()
        deletions = parse_deletions(data)
        if not deletions:
            return result
        received = _received_instant(data)
        kept_types = {DELETED_KEY, DELETED_RECORDS_TYPE_NAME}
        stored_days = await self.get_stored_days()
        for type_name, ids in sorted(deletions.items()):
            matched: set[str] = set()
            record_type = record_type_of(type_name)
            # The phone cannot delete the record of what was deleted.
            usable = _TYPE_NAME_RE.fullmatch(type_name) and type_name not in kept_types
            for stored_type, date in stored_days if usable else []:
                if stored_type != record_type:
                    continue
                key = self._key(record_type, date)
                raw: dict = await redis_api.hgetall(key)
                fields: list[str] = []
                removed: list[Any] = []
                for k, v in raw.items():
                    try:
                        record = json.loads(_decode(v))
                    except ValueError:
                        continue
                    record_id = _record_id(record)
                    if record_id in ids:
                        fields.append(_decode(k))
                        removed.append(record)
                        matched.add(record_id)
                if not fields:
                    continue
                kept_dates = await self.add_records(
                    DELETED_RECORDS_TYPE_NAME,
                    [{"type": type_name, "record": record} for record in removed],
                    received,
                )
                await redis_api.hdel(key, *fields)
                result.removed_records += len(fields)
                result.touched.setdefault(record_type, set()).add(date)
                result.touched.setdefault(
                    record_type_of(DELETED_RECORDS_TYPE_NAME), set()
                ).update(kept_dates)
                if len(fields) == len(raw):
                    result.emptied.add((record_type, date))
            result.unmatched_ids += len(ids - matched)
        return result

    async def add_heart_rate(
        self, samples: Iterable[HeartRateSample], record_type: str = "heart-rate"
    ) -> set[datetime.date]:
        """Store samples and return the set of local dates that changed."""
        return await self.add_records(
            type_name_of(record_type),
            [
                {"bpm": sample.bpm, "time": _format_instant(sample.time)}
                for sample in samples
            ],
        )

    async def add_resting_heart_rate(
        self, samples: Iterable[HeartRateSample]
    ) -> set[datetime.date]:
        """Store resting samples and return the local dates that changed."""
        return await self.add_heart_rate(samples, record_type="resting-heart-rate")

    async def add_steps(self, records: Iterable[StepsRecord]) -> set[datetime.date]:
        """Store step intervals and return the local dates that changed.

        An interval belongs to the local date of its start time.
        """
        return await self.add_records(
            "steps",
            [
                {
                    "count": record.count,
                    "start_time": _format_instant(record.start_time),
                    "end_time": _format_instant(record.end_time),
                }
                for record in records
            ],
        )

    @property
    def _export_pending_key(self) -> str:
        return f"health-connect:export-pending:{self._user.id}"

    async def mark_export_pending(
        self, days: Iterable[tuple[str, datetime.date]]
    ) -> None:
        """Remember that the raw data files of these days are out of date.

        Each day is a record type of the store and a local date.
        """
        members = [f"{record_type}|{date.isoformat()}" for record_type, date in days]
        if members:
            await redis_api.sadd(self._export_pending_key, *members)

    async def clear_export_pending(self, record_type: str, date: datetime.date) -> None:
        """Forget that the raw data file of this day is out of date."""
        await redis_api.srem(
            self._export_pending_key, f"{record_type}|{date.isoformat()}"
        )

    @property
    def _export_emptied_key(self) -> str:
        return f"health-connect:export-emptied:{self._user.id}"

    async def mark_export_emptied(
        self, days: Iterable[tuple[str, datetime.date]]
    ) -> None:
        """Remember that deletions on the phone left these days with no records.

        The raw data file of such a day is deleted when the day is exported.
        A day that has no records for another reason keeps its file.
        """
        members = [f"{record_type}|{date.isoformat()}" for record_type, date in days]
        if members:
            await redis_api.sadd(self._export_emptied_key, *members)

    async def is_export_emptied(self, record_type: str, date: datetime.date) -> bool:
        """Return whether deletions on the phone left this day with no records."""
        return bool(
            await redis_api.sismember(
                self._export_emptied_key, f"{record_type}|{date.isoformat()}"
            )
        )

    async def clear_export_emptied(self, record_type: str, date: datetime.date) -> None:
        """Forget that deletions on the phone left this day with no records."""
        await redis_api.srem(
            self._export_emptied_key, f"{record_type}|{date.isoformat()}"
        )

    async def get_export_pending(self) -> list[tuple[str, datetime.date]]:
        """Return the days whose raw data files are out of date.

        Sort the pairs by record type, then by date.
        """
        days: set[tuple[str, datetime.date]] = set()
        for member in await redis_api.smembers(self._export_pending_key):
            record_type, _, day = _decode(member).rpartition("|")
            try:
                days.add((record_type, datetime.date.fromisoformat(day)))
            except ValueError:
                continue
        return sorted(days)

    async def get_stored_days(self) -> list[tuple[str, datetime.date]]:
        """Return every record type and local date that has stored records.

        Sort the pairs by record type, then by date.
        """
        days: set[tuple[str, datetime.date]] = set()
        async for key in redis_api.scan_iter(match=f"{self._prefix}*"):
            record_type, _, day = _decode(key)[len(self._prefix) :].rpartition(":")
            try:
                days.add((record_type, datetime.date.fromisoformat(day)))
            except ValueError:
                continue
        return sorted(days)

    async def delete_dates(self, dates: Iterable[datetime.date]) -> int:
        """Delete every stored record of these local dates.

        Remove the records of every record type. Return the number of Redis
        keys that existed and were deleted.
        """
        wanted = set(dates)
        keys = [
            self._key(record_type, date)
            for record_type, date in await self.get_stored_days()
            if date in wanted
        ]
        if not keys:
            return 0
        return int(await redis_api.delete(*keys))

    async def _get_timed(self, record_type: str, date: datetime.date) -> dict[int, Any]:
        """Return the well-formed records of a parsed type, by epoch ms.

        A value is the record as a dict, or the string of the older storage
        format.
        """
        raw: dict = await redis_api.hgetall(self._key(record_type, date))
        timed: dict[int, Any] = {}
        for k, v in raw.items():
            field, value = _decode(k), _decode(v)
            if not field.isdigit():
                continue
            try:
                record = json.loads(value)
            except ValueError:
                record = None
            timed[int(field)] = record if isinstance(record, dict) else value
        return timed

    async def get_heart_rate(
        self, date: datetime.date, record_type: str = "heart-rate"
    ) -> list[HeartRateSample]:
        """Return the samples of one local date, oldest first."""
        samples = []
        for ms, record in (await self._get_timed(record_type, date)).items():
            if isinstance(record, dict):
                samples.append(_heart_rate_sample(record))
            else:
                samples.append(HeartRateSample(_from_epoch_ms(str(ms)), int(record)))
        return sorted(samples, key=lambda s: s.time)

    async def get_resting_heart_rate(
        self, date: datetime.date
    ) -> list[HeartRateSample]:
        """Return the resting samples of one local date, oldest first."""
        return await self.get_heart_rate(date, record_type="resting-heart-rate")

    async def get_heart_rate_dates(self) -> list[datetime.date]:
        """Return every local date that has stored heart rate records.

        Include dates that have only resting heart rate records. Sort the
        dates from oldest to newest.
        """
        return sorted(
            {
                date
                for record_type, date in await self.get_stored_days()
                if record_type in ("heart-rate", "resting-heart-rate")
            }
        )

    async def get_steps(self, date: datetime.date) -> list[StepsRecord]:
        """Return the step intervals of one local date, oldest first."""
        records = []
        for ms, record in (await self._get_timed("steps", date)).items():
            if isinstance(record, dict):
                records.append(_steps_record(record))
            else:
                end_ms, count = record.split(":")
                records.append(
                    StepsRecord(
                        start_time=_from_epoch_ms(str(ms)),
                        end_time=_from_epoch_ms(end_ms),
                        count=int(count),
                    )
                )
        return sorted(records, key=lambda r: r.start_time)

    async def get_raw_records(self, record_type: str, date: datetime.date) -> list[Any]:
        """Return every stored record of one type and local date.

        Return each record as the JSON value the phone sent, oldest first. A
        record with no timestamp comes last. Rebuild a record of the older
        storage format from the fields that were kept.
        """
        raw: dict = await redis_api.hgetall(self._key(record_type, date))
        keyed: list[tuple[float, str, Any]] = []
        for k, v in raw.items():
            field, value = _decode(k), _decode(v)
            try:
                record = json.loads(value)
            except ValueError:
                record = value
            if field.isdigit() and not isinstance(record, dict):
                start = _from_epoch_ms(field)
                if record_type == "steps":
                    end_ms, count = value.split(":")
                    record = {
                        "count": int(count),
                        "start_time": _format_instant(start),
                        "end_time": _format_instant(_from_epoch_ms(end_ms)),
                    }
                else:
                    record = {"bpm": int(value), "time": _format_instant(start)}
            instant = _record_instant(record)
            order = _epoch_ms(instant) if instant is not None else float("inf")
            keyed.append((order, field, record))
        return [record for _, _, record in sorted(keyed, key=lambda item: item[:2])]
