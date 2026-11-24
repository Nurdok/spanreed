"""Tests of the jobs that a plugin keeps running for its users."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed import service
from spanreed.test_utils import mock_user_find_by_id


def _plugin(*looks: Any) -> MagicMock:
    """Return a plugin whose users are these, one set for each look.

    A look that is an exception fails. After the last look the test is
    over.
    """
    plugin = MagicMock()
    plugin.canonical_name.return_value = "listener"
    left = list(looks)

    async def user_ids() -> set[int]:
        if not left:
            raise asyncio.CancelledError()
        found = left.pop(0)
        if isinstance(found, Exception):
            raise found
        return set(found)

    plugin.user_ids = user_ids
    return plugin


async def _breathe(seconds: float = 0) -> None:
    """Stand in for a wait: let the jobs run, and take no time."""
    for _ in range(5):
        await asyncio.sleep(0)


def _serve(plugin: MagicMock, serve: Any) -> None:
    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await service.serve_users(plugin, serve)

    with patch("spanreed.service._sleep", new=_breathe), patch(
        "spanreed.service.User.find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        asyncio.run(run())


# -- who is served ------------------------------------------------------------


def test_each_user_of_the_plugin_gets_a_job_of_their_own() -> None:
    served: list[int] = []

    async def serve(user: Any) -> None:
        served.append(user.id)
        await asyncio.Event().wait()

    _serve(_plugin({4, 9}, {4, 9}), serve)

    # Once each, however often the users are read.
    assert served == [4, 9]


def test_a_user_who_registers_later_is_served_without_a_restart() -> None:
    served: list[int] = []

    async def serve(user: Any) -> None:
        served.append(user.id)
        await asyncio.Event().wait()

    _serve(_plugin({4}, {4}, {4, 9}), serve)

    assert served == [4, 9]


def test_the_job_of_a_user_who_left_is_cancelled_and_starts_if_they_come_back() -> None:
    events: list[str] = []

    async def serve(user: Any) -> None:
        events.append(f"start {user.id}")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append(f"stop {user.id}")
            raise

    _serve(_plugin({4, 9}, {4}, {4, 9}), serve)

    assert events[:4] == ["start 4", "start 9", "stop 9", "start 9"]
    # When the plugin stops, every job does.
    assert sorted(events[4:]) == ["stop 4", "stop 9"]


def test_a_job_that_returns_is_done() -> None:
    served: list[int] = []

    async def serve(user: Any) -> None:
        served.append(user.id)

    _serve(_plugin({4}, {4}, {4}), serve)

    assert served == [4]


def test_serving_goes_on_when_the_users_cannot_be_read() -> None:
    served: list[int] = []

    async def serve(user: Any) -> None:
        served.append(user.id)
        await asyncio.Event().wait()

    _serve(_plugin({4}, ConnectionError("Redis is away"), {4, 9}), serve)

    assert served == [4, 9]


# -- a job that fails -----------------------------------------------------------


def _pauses(outcomes: list[BaseException | None], clock: Any = None) -> list[float]:
    """Run one user's job with these outcomes, and return the pauses between.

    An outcome is what the job raises, or ``None`` for a job that returns.
    """
    pauses: list[float] = []
    left = list(outcomes)

    async def serve(user: Any) -> None:
        outcome = left.pop(0)
        if outcome is not None:
            raise outcome

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    plugin = _plugin()
    with patch("spanreed.service._sleep", new=sleep), patch(
        "spanreed.service._monotonic", clock or (lambda: 0.0)
    ):
        asyncio.run(service._keep_serving(plugin, serve, mock_user_find_by_id(4)))
    return pauses


def test_a_job_that_fails_starts_again_after_a_pause_that_doubles() -> None:
    pauses = _pauses([ValueError("boom")] * 7 + [None])

    assert pauses == [60, 120, 240, 480, 960, 1800, 1800]


def test_the_pauses_start_over_after_a_job_ran_for_long() -> None:
    # The third job started at 200 and failed at 1000: it ran for long
    # enough to count as working.
    times = iter([0.0, 1.0, 100.0, 101.0, 200.0, 1000.0, 1100.0, 1101.0])

    pauses = _pauses(
        [ValueError("a"), ValueError("b"), ValueError("c"), None],
        clock=lambda: next(times),
    )

    assert pauses == [60, 120, 60]


def test_a_job_that_is_cancelled_is_not_started_again() -> None:
    with pytest.raises(asyncio.CancelledError):
        _pauses([asyncio.CancelledError(), None])
