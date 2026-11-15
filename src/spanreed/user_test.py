import asyncio
import datetime
import zoneinfo
from unittest.mock import patch

import pytest

from spanreed.test_utils import FakeRedis
from spanreed.user import User


def test_parse_timezone_accepts_iana_names() -> None:
    tz = User.parse_timezone("Asia/Jerusalem")
    assert isinstance(tz, zoneinfo.ZoneInfo)
    assert tz.key == "Asia/Jerusalem"
    # Surrounding whitespace is tolerated (pasted from a chat).
    assert str(User.parse_timezone("  Europe/London ")) == "Europe/London"


@pytest.mark.parametrize("name", ["", "garbage/zone", "asia/jerusalem", "UTC+3"])
def test_parse_timezone_rejects_unknown_names(name: str) -> None:
    with pytest.raises(ValueError):
        User.parse_timezone(name)


def test_tzinfo_falls_back_to_server_local_when_unset() -> None:
    user = User()
    assert user.timezone is None
    # Must be a usable tzinfo even when nothing is configured.
    now = datetime.datetime.now(tz=user.tzinfo)
    assert now.tzinfo is not None


def test_set_timezone_validates_and_persists() -> None:
    fake = FakeRedis()
    user = User()
    user.id = 4

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(user.set_timezone(" America/New_York "))

    assert user.timezone == "America/New_York"
    assert str(user.tzinfo) == "America/New_York"
    assert fake.strings["user:4:timezone"] == "America/New_York"


def test_set_timezone_rejects_bad_name_without_storing() -> None:
    fake = FakeRedis()
    user = User()
    user.id = 4

    with patch("spanreed.user.redis_api", new=fake):
        with pytest.raises(ValueError):
            asyncio.run(user.set_timezone("Mars/Olympus_Mons"))

    assert user.timezone is None
    assert "user:4:timezone" not in fake.strings


def test_find_by_id_loads_timezone() -> None:
    fake = FakeRedis()
    fake.strings["user:4:name"] = b"Test User"
    fake.strings["user:4:timezone"] = b"Asia/Jerusalem"

    with patch("spanreed.user.redis_api", new=fake):
        user = asyncio.run(User.find_by_id(4))

    assert user.name == "Test User"
    assert user.timezone == "Asia/Jerusalem"


def test_find_by_id_without_timezone() -> None:
    fake = FakeRedis()
    fake.strings["user:4:name"] = b"Test User"

    with patch("spanreed.user.redis_api", new=fake):
        user = asyncio.run(User.find_by_id(4))

    assert user.timezone is None


def test_parse_birth_date_accepts_iso_dates() -> None:
    assert User.parse_birth_date(" 1990-04-23 ") == datetime.date(1990, 4, 23)


@pytest.mark.parametrize(
    "text", ["", "23/04/1990", "1990-13-01", "2999-01-01", "1800-01-01"]
)
def test_parse_birth_date_rejects_other_text_and_implausible_dates(text: str) -> None:
    with pytest.raises(ValueError):
        User.parse_birth_date(text)


def test_age_on_counts_whole_years() -> None:
    user = User()
    assert user.age_on(datetime.date(2026, 5, 9)) is None
    user.birth_date = datetime.date(1990, 4, 23)

    assert user.age_on(datetime.date(2026, 4, 22)) == 35
    assert user.age_on(datetime.date(2026, 4, 23)) == 36
    assert user.age_on(datetime.date(1990, 4, 23)) == 0
    assert user.age_on(datetime.date(1990, 4, 22)) is None


def test_set_birth_date_validates_and_persists() -> None:
    fake = FakeRedis()
    user = User()
    user.id = 4

    with patch("spanreed.user.redis_api", new=fake):
        asyncio.run(user.set_birth_date("1990-04-23"))
        with pytest.raises(ValueError):
            asyncio.run(user.set_birth_date("soon"))
        stored = asyncio.run(User.get_user_birth_date(4))
        missing = asyncio.run(User.get_user_birth_date(5))

    assert user.birth_date == datetime.date(1990, 4, 23)
    assert fake.strings["user:4:birth_date"] == "1990-04-23"
    assert stored == datetime.date(1990, 4, 23)
    assert missing is None


def test_find_by_id_loads_birth_date() -> None:
    fake = FakeRedis()
    fake.strings["user:4:name"] = b"Test"
    fake.strings["user:4:birth_date"] = b"1990-04-23"

    with patch("spanreed.user.redis_api", new=fake):
        user = asyncio.run(User.find_by_id(4))

    assert user.birth_date == datetime.date(1990, 4, 23)
