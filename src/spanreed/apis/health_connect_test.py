import asyncio
import datetime
import json
from unittest.mock import patch

import pytest

from spanreed.apis.health_connect import (
    INBOX_MAX_LENGTH,
    HealthConnectApi,
    HealthConnectStore,
    HeartRateSample,
    StepsRecord,
    daily_heart_rate_metrics,
    daily_step_total,
    local_date,
    parse_instant,
    parse_payload,
    percentile,
)
from spanreed.test_utils import FakeRedis, mock_user_find_by_id


UTC = datetime.timezone.utc


# --- parse_instant / parse_payload -------------------------------------------


def test_parse_instant_accepts_java_instant_formats() -> None:
    assert parse_instant("2026-05-09T12:34:56Z") == datetime.datetime(
        2026, 5, 9, 12, 34, 56, tzinfo=UTC
    )
    assert parse_instant("2026-05-09T12:34:56.789Z") == datetime.datetime(
        2026, 5, 9, 12, 34, 56, 789000, tzinfo=UTC
    )
    # Nanosecond precision is truncated to microseconds.
    assert parse_instant("2026-05-09T12:34:56.123456789Z") == datetime.datetime(
        2026, 5, 9, 12, 34, 56, 123456, tzinfo=UTC
    )


def test_parse_instant_assumes_utc_for_naive_strings() -> None:
    assert parse_instant("2026-05-09T12:34:56").tzinfo == UTC


def test_parse_instant_rejects_non_strings() -> None:
    with pytest.raises(ValueError):
        parse_instant(1700000000)


def test_parse_payload_full_example() -> None:
    data = {
        "timestamp": "2026-05-09T14:00:00.123Z",
        "app_version": "1.2.3",
        "steps": [
            {
                "count": 8421,
                "start_time": "2026-05-08T00:00:00Z",
                "end_time": "2026-05-09T00:00:00Z",
            }
        ],
        "heart_rate": [
            {"bpm": 72, "time": "2026-05-09T08:15:00Z"},
            {"bpm": 65, "time": "2026-05-09T08:16:00Z"},
        ],
        "resting_heart_rate": [{"bpm": 58, "time": "2026-05-09T06:00:00Z"}],
    }

    payload = parse_payload(data)

    assert payload.app_version == "1.2.3"
    assert payload.steps == [
        StepsRecord(
            start_time=datetime.datetime(2026, 5, 8, tzinfo=UTC),
            end_time=datetime.datetime(2026, 5, 9, tzinfo=UTC),
            count=8421,
        )
    ]
    assert payload.heart_rate == [
        HeartRateSample(datetime.datetime(2026, 5, 9, 8, 15, tzinfo=UTC), 72),
        HeartRateSample(datetime.datetime(2026, 5, 9, 8, 16, tzinfo=UTC), 65),
    ]
    assert payload.resting_heart_rate == [
        HeartRateSample(datetime.datetime(2026, 5, 9, 6, 0, tzinfo=UTC), 58)
    ]
    assert payload.unknown_types == []
    assert payload.skipped_records == 0
    assert not payload.is_empty()


def test_parse_payload_metadata_only_is_empty() -> None:
    payload = parse_payload({"timestamp": "2026-05-09T14:00:00Z", "app_version": "1"})
    assert payload.is_empty()
    assert payload.unknown_types == []


def test_parse_payload_skips_malformed_records_but_keeps_the_rest() -> None:
    data = {
        "heart_rate": [
            {"bpm": 72, "time": "2026-05-09T08:15:00Z"},
            {"bpm": "not-a-number", "time": "2026-05-09T08:16:00Z"},
            {"time": "2026-05-09T08:17:00Z"},  # missing bpm
            "garbage",
            {"bpm": 70, "time": "2026-05-09T08:18:00Z"},
        ],
        "steps": [
            {"count": 10, "start_time": "2026-05-09T08:00:00Z"},  # missing end
            {
                "count": 20,
                "start_time": "2026-05-09T09:00:00Z",
                "end_time": "2026-05-09T09:10:00Z",
            },
        ],
    }

    payload = parse_payload(data)

    assert [s.bpm for s in payload.heart_rate] == [72, 70]
    assert [r.count for r in payload.steps] == [20]
    assert payload.skipped_records == 4


def test_parse_payload_ignores_non_list_types() -> None:
    payload = parse_payload({"heart_rate": {"bpm": 72}, "steps": "nope"})
    assert payload.is_empty()
    assert payload.skipped_records == 0


def test_parse_payload_collects_unknown_types() -> None:
    payload = parse_payload(
        {
            "timestamp": "2026-05-09T14:00:00Z",
            "sleep": [{"start_time": "x", "end_time": "y"}],
            "weight": [{"kg": 70.0, "time": "z"}],
        }
    )
    assert payload.unknown_types == ["sleep", "weight"]
    assert payload.is_empty()


def test_parse_payload_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        parse_payload([1, 2, 3])


# --- local_date --------------------------------------------------------------


def test_local_date_buckets_by_timezone() -> None:
    # 23:30 UTC on the 8th is already the 9th in UTC+3.
    instant = datetime.datetime(2026, 5, 8, 23, 30, tzinfo=UTC)
    plus3 = datetime.timezone(datetime.timedelta(hours=3))
    assert local_date(instant, plus3) == datetime.date(2026, 5, 9)
    assert local_date(instant, UTC) == datetime.date(2026, 5, 8)


# --- daily_step_total --------------------------------------------------------


def test_daily_step_total_sums_counts() -> None:
    t = datetime.datetime(2026, 5, 9, 8, 0, tzinfo=UTC)
    records = [
        StepsRecord(t, t + datetime.timedelta(minutes=10), 120),
        StepsRecord(
            t + datetime.timedelta(hours=1),
            t + datetime.timedelta(hours=1, minutes=5),
            30,
        ),
    ]
    assert daily_step_total(records) == 150
    assert daily_step_total([]) == 0


# --- HealthConnectStore ------------------------------------------------------


PLUS3 = datetime.timezone(datetime.timedelta(hours=3))


def test_store_heart_rate_roundtrip_and_date_bucketing() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    samples = [
        # 23:30 UTC on the 8th → the 9th in UTC+3.
        HeartRateSample(datetime.datetime(2026, 5, 8, 23, 30, tzinfo=UTC), 60),
        HeartRateSample(datetime.datetime(2026, 5, 9, 8, 15, tzinfo=UTC), 72),
        HeartRateSample(datetime.datetime(2026, 5, 8, 10, 0, tzinfo=UTC), 80),
    ]

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=PLUS3)
        touched = asyncio.run(store.add_heart_rate(samples))
        day8 = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 8)))
        day9 = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 9)))
        day10 = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 10)))

    assert touched == {datetime.date(2026, 5, 8), datetime.date(2026, 5, 9)}
    assert day8 == [samples[2]]
    # Sorted by time on read.
    assert day9 == [samples[0], samples[1]]
    assert day10 == []
    # Every touched day hash gets a TTL.
    assert fake.expirations == {
        "health-connect:4:heart-rate:2026-05-08": 30 * 24 * 3600,
        "health-connect:4:heart-rate:2026-05-09": 30 * 24 * 3600,
    }


def test_store_redelivered_sample_overwrites_in_place() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    t = datetime.datetime(2026, 5, 9, 8, 15, tzinfo=UTC)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=UTC)
        asyncio.run(store.add_heart_rate([HeartRateSample(t, 72)]))
        # Same instant re-sent (watermark overlap), with a corrected value.
        asyncio.run(store.add_heart_rate([HeartRateSample(t, 70)]))
        day = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 9)))

    assert day == [HeartRateSample(t, 70)]


def test_store_heart_rate_dates_cover_both_record_types_of_one_user() -> None:
    fake = FakeRedis()
    store = HealthConnectStore(mock_user_find_by_id(4), tz=UTC)
    other = HealthConnectStore(mock_user_find_by_id(40), tz=UTC)

    def at(day: int) -> datetime.datetime:
        return datetime.datetime(2026, 5, day, 8, 0, tzinfo=UTC)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        assert asyncio.run(store.get_heart_rate_dates()) == []
        asyncio.run(store.add_heart_rate([HeartRateSample(at(9), 72)]))
        asyncio.run(store.add_heart_rate([HeartRateSample(at(2), 70)]))
        asyncio.run(store.add_resting_heart_rate([HeartRateSample(at(9), 58)]))
        asyncio.run(store.add_resting_heart_rate([HeartRateSample(at(5), 57)]))
        asyncio.run(other.add_heart_rate([HeartRateSample(at(20), 80)]))
        dates = asyncio.run(store.get_heart_rate_dates())

    assert dates == [
        datetime.date(2026, 5, 2),
        datetime.date(2026, 5, 5),
        datetime.date(2026, 5, 9),
    ]


def test_store_resting_heart_rate_is_kept_separately() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    t = datetime.datetime(2026, 5, 9, 8, 15, tzinfo=UTC)

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=UTC)
        asyncio.run(store.add_heart_rate([HeartRateSample(t, 72)]))
        asyncio.run(store.add_resting_heart_rate([HeartRateSample(t, 58)]))
        hr = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 9)))
        rhr = asyncio.run(store.get_resting_heart_rate(datetime.date(2026, 5, 9)))

    assert [s.bpm for s in hr] == [72]
    assert [s.bpm for s in rhr] == [58]


def test_store_steps_roundtrip_bucketed_by_start() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    records = [
        StepsRecord(
            start_time=datetime.datetime(2026, 5, 9, 8, 0, tzinfo=UTC),
            end_time=datetime.datetime(2026, 5, 9, 8, 10, tzinfo=UTC),
            count=120,
        ),
        # Straddles midnight UTC; bucketed on its start date.
        StepsRecord(
            start_time=datetime.datetime(2026, 5, 9, 23, 55, tzinfo=UTC),
            end_time=datetime.datetime(2026, 5, 10, 0, 5, tzinfo=UTC),
            count=30,
        ),
    ]

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=UTC)
        touched = asyncio.run(store.add_steps(records))
        day9 = asyncio.run(store.get_steps(datetime.date(2026, 5, 9)))
        day10 = asyncio.run(store.get_steps(datetime.date(2026, 5, 10)))

    assert touched == {datetime.date(2026, 5, 9)}
    assert day9 == records
    assert day10 == []


def test_store_decodes_bytes_from_real_redis() -> None:
    """The real client returns bytes for hash fields and values."""
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    t = datetime.datetime(2026, 5, 9, 8, 15, tzinfo=UTC)
    epoch_ms = int(t.timestamp() * 1000)
    fake.hashes["health-connect:4:heart-rate:2026-05-09"] = {
        str(epoch_ms).encode(): b"72"
    }
    fake.hashes["health-connect:4:steps:2026-05-09"] = {
        str(epoch_ms).encode(): f"{epoch_ms + 60_000}:15".encode()
    }

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        store = HealthConnectStore(user, tz=UTC)
        hr = asyncio.run(store.get_heart_rate(datetime.date(2026, 5, 9)))
        steps = asyncio.run(store.get_steps(datetime.date(2026, 5, 9)))

    assert hr == [HeartRateSample(t, 72)]
    assert steps == [
        StepsRecord(start_time=t, end_time=t + datetime.timedelta(minutes=1), count=15)
    ]


# --- HealthConnectApi (token registry + inbox) -------------------------------


def test_token_registry_roundtrip() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    token = HealthConnectApi.generate_token()

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        assert asyncio.run(HealthConnectApi.user_id_for_token(token)) is None
        asyncio.run(HealthConnectApi.register_token(user, token))
        assert asyncio.run(HealthConnectApi.user_id_for_token(token)) == 4
        assert asyncio.run(HealthConnectApi.user_id_for_token("")) is None
        asyncio.run(HealthConnectApi.unregister_token(token))
        assert asyncio.run(HealthConnectApi.user_id_for_token(token)) is None

    # The raw token never appears in a key name.
    assert all(token not in key for key in fake.strings)


def test_generate_token_is_unique_and_long() -> None:
    a = HealthConnectApi.generate_token()
    b = HealthConnectApi.generate_token()
    assert a != b
    assert len(a) >= 40


def test_handle_webhook_rejects_unknown_token_without_queueing() -> None:
    fake = FakeRedis()
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        status = asyncio.run(HealthConnectApi.handle_webhook("nope", "{}"))
    assert status == 401
    assert fake.lists == {}


def test_handle_webhook_rejects_bad_json() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    token = "t0k3n"
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(HealthConnectApi.register_token(user, token))
        assert asyncio.run(HealthConnectApi.handle_webhook(token, "{not json")) == 400
        assert asyncio.run(HealthConnectApi.handle_webhook(token, "[1, 2]")) == 400
    assert fake.lists == {}


def test_handle_webhook_queues_raw_body_for_user() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    token = "t0k3n"
    body = json.dumps({"heart_rate": [{"bpm": 72, "time": "2026-05-09T08:15:00Z"}]})

    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(HealthConnectApi.register_token(user, token))
        status = asyncio.run(HealthConnectApi.handle_webhook(token, body))
        # The plugin drains oldest-first.
        dequeued = asyncio.run(HealthConnectApi.dequeue_payload(user))
        empty = asyncio.run(HealthConnectApi.dequeue_payload(user))

    assert status == 202
    assert dequeued == body
    assert empty is None


def test_dequeue_preserves_delivery_order() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        asyncio.run(HealthConnectApi.enqueue_payload(4, "first"))
        asyncio.run(HealthConnectApi.enqueue_payload(4, "second"))
        assert asyncio.run(HealthConnectApi.dequeue_payload(user)) == "first"
        assert asyncio.run(HealthConnectApi.dequeue_payload(user)) == "second"


def test_dequeue_decodes_bytes() -> None:
    fake = FakeRedis()
    user = mock_user_find_by_id(4)
    fake.lists[HealthConnectApi.inbox_key(4)] = [b"{}"]
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        assert asyncio.run(HealthConnectApi.dequeue_payload(user)) == "{}"


def test_inbox_is_capped() -> None:
    fake = FakeRedis()
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        for i in range(INBOX_MAX_LENGTH + 10):
            asyncio.run(HealthConnectApi.enqueue_payload(4, str(i)))
    inbox = fake.lists[HealthConnectApi.inbox_key(4)]
    assert len(inbox) == INBOX_MAX_LENGTH
    # Newest kept at the head, oldest dropped from the tail.
    assert inbox[0] == str(INBOX_MAX_LENGTH + 9)
    assert inbox[-1] == "10"


@pytest.mark.parametrize(
    "header, expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER  abc ", "abc"),
        ("Bearer `abc`", "abc"),
        ("  Bearer abc\n", "abc"),
        ("abc", ""),
        ("Basic abc", ""),
        ("", ""),
    ],
)
def test_token_from_header(header: str, expected: str) -> None:
    assert HealthConnectApi.token_from_header(header) == expected


# --- daily heart rate metrics --------------------------------------------------


def test_percentile_interpolates_like_numpy() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 95) == 10
    assert percentile([5, 1, 3], 0) == 1
    assert percentile([5, 1, 3], 100) == 5
    with pytest.raises(ValueError):
        percentile([], 50)


def test_daily_heart_rate_metrics() -> None:
    t = datetime.datetime(2026, 5, 9, 8, 0, tzinfo=datetime.timezone.utc)
    hr = [HeartRateSample(t, b) for b in range(60, 161)]
    resting = [
        HeartRateSample(t + datetime.timedelta(hours=2), 54),
        HeartRateSample(t, 58),
    ]
    assert daily_heart_rate_metrics(hr, resting) == {
        "heart-rate-p5": 65,
        "heart-rate-avg": 110,
        "heart-rate-p95": 155,
        "heart-rate-p98": 158,
        "heart-rate-p99": 159,
        "resting-heart-rate": 54,
    }


def test_daily_heart_rate_metrics_leaves_out_missing_data() -> None:
    t = datetime.datetime(2026, 5, 9, 8, 0, tzinfo=datetime.timezone.utc)
    assert daily_heart_rate_metrics([], []) == {}
    assert daily_heart_rate_metrics([], [HeartRateSample(t, 50)]) == {
        "resting-heart-rate": 50
    }
