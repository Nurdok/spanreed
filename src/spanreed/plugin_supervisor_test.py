import asyncio
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter

from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id


class _CrashyPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Crashy"


def _make_plugin() -> _CrashyPlugin:
    Plugin.reset_registry()
    return _CrashyPlugin()


def _run_supervised_capturing_sleeps(
    plugin: Plugin, run_results: list[BaseException | None]
) -> list[float]:
    """Run the supervisor with scripted run_for_user outcomes.

    Each entry in run_results is an exception to raise (or None to return
    normally, which stops the supervisor). Returns the sleep durations the
    supervisor requested between restarts.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    outcomes = iter(run_results)

    async def fake_run_for_user(user: object) -> None:
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    plugin.run_for_user = fake_run_for_user  # type: ignore[method-assign]
    user = mock_user_find_by_id(1)
    with patch("spanreed.plugin.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
        asyncio.run(plugin._run_for_user_supervised(user))
    return sleeps


def test_crash_backoff_doubles_and_caps() -> None:
    plugin = _make_plugin()
    crashes: list[BaseException | None] = [ValueError("boom")] * 7 + [None]

    sleeps = _run_supervised_capturing_sleeps(plugin, crashes)

    assert sleeps == [60, 120, 240, 480, 960, 1800, 1800]


def test_flood_control_sleep_is_capped_and_jittered() -> None:
    plugin = _make_plugin()
    ban = RetryAfter(26000)  # multi-hour ban

    sleeps = _run_supervised_capturing_sleeps(plugin, [ban, None])

    assert len(sleeps) == 1
    lo, hi = Plugin.FLOOD_WAKE_JITTER_RANGE_S
    assert 3600 + lo <= sleeps[0] <= 3600 + hi


def test_flood_control_jitter_desynchronizes_plugins() -> None:
    # Two plugins hitting the same ban must not pick the same wake time.
    plugin = _make_plugin()
    ban = RetryAfter(26000)
    sleeps_a = _run_supervised_capturing_sleeps(plugin, [ban, None])
    sleeps_b = _run_supervised_capturing_sleeps(plugin, [ban, None])

    assert sleeps_a[0] != sleeps_b[0]


def test_short_flood_ban_honors_retry_after() -> None:
    plugin = _make_plugin()
    ban = RetryAfter(120)

    sleeps = _run_supervised_capturing_sleeps(plugin, [ban, None])

    lo, hi = Plugin.FLOOD_WAKE_JITTER_RANGE_S
    assert 120 + lo <= sleeps[0] <= 120 + hi


def test_backoff_resets_after_long_uptime() -> None:
    plugin = _make_plugin()
    # Simulate uptime by advancing a fake monotonic clock: each run "lasts"
    # long enough to reset backoff on the third crash.
    times = iter([0.0, 1.0, 100.0, 101.0, 200.0, 1000.0, 1100.0, 1101.0])
    crashes: list[BaseException | None] = [
        ValueError("a"),
        ValueError("b"),
        ValueError("c"),
        None,
    ]
    with patch("spanreed.plugin._monotonic", side_effect=times):
        sleeps = _run_supervised_capturing_sleeps(plugin, crashes)

    # First two crashes back off 60 -> 120; the third run started at t=200
    # and crashed at t=1000 (800s uptime > reset threshold), so it restarts
    # at the initial 60s again.
    assert sleeps == [60, 120, 60]
