"""Tests of the list of errors that Spanreed keeps."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from spanreed import errors
from spanreed.test_utils import FakeRedis


def test_the_newest_error_comes_first() -> None:
    redis = FakeRedis()
    with patch("spanreed.errors.redis_api", new=redis):

        async def run() -> list[str]:
            await errors.report("the first")
            await errors.report("the second")
            return [await errors.next_report(), await errors.next_report()]

        assert asyncio.run(run()) == ["the second", "the first"]


def test_an_error_from_redis_is_read_as_text() -> None:
    redis = FakeRedis()
    redis.lists[errors.QUEUE] = [b"ValueError: caf\xc3\xa9\n"]
    with patch("spanreed.errors.redis_api", new=redis):
        assert asyncio.run(errors.next_report()) == "ValueError: café\n"


def test_the_wait_for_an_error_starts_again_when_it_is_over() -> None:
    redis = AsyncMock()
    redis.blpop.side_effect = [None, None, (errors.QUEUE, b"late")]

    with patch("spanreed.errors.redis_api", new=redis):
        assert asyncio.run(errors.next_report()) == "late"

    assert redis.blpop.await_count == 3
    redis.blpop.assert_awaited_with(errors.QUEUE, timeout=errors.WAIT_S)


def test_the_errors_that_wait_are_dropped() -> None:
    redis = FakeRedis()
    with patch("spanreed.errors.redis_api", new=redis):

        async def run() -> None:
            await errors.report("one")
            await errors.report("two")
            await errors.drop_waiting()

        asyncio.run(run())
    assert not redis.lists.get(errors.QUEUE)


def test_an_error_that_is_kept_back_is_reported() -> None:
    redis = FakeRedis()
    with patch("spanreed.errors.redis_api", new=redis):

        async def run() -> bool:
            async with errors.suppress_and_log_exception(ValueError) as kept_back:
                raise ValueError("boom")
            return bool(kept_back)

        assert asyncio.run(run()) is True
    (text,) = redis.lists[errors.QUEUE]
    assert text.endswith("ValueError: boom\n")


def test_an_error_of_another_type_goes_on_and_is_not_reported() -> None:
    redis = FakeRedis()
    with patch("spanreed.errors.redis_api", new=redis):

        async def run() -> None:
            async with errors.suppress_and_log_exception(ValueError):
                raise KeyError("other")

        with pytest.raises(KeyError):
            asyncio.run(run())
    assert not redis.lists.get(errors.QUEUE)


def test_no_error_is_not_reported() -> None:
    redis = FakeRedis()
    with patch("spanreed.errors.redis_api", new=redis):

        async def run() -> bool:
            async with errors.suppress_and_log_exception(ValueError) as kept_back:
                pass
            return bool(kept_back)

        assert asyncio.run(run()) is False
    assert not redis.lists.get(errors.QUEUE)
