"""Tests of what is read from the Spanreed plugin for Obsidian."""

import asyncio
import datetime
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import dateutil.tz
import pytest

from spanreed.apis import obsidian_watch
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.apis.obsidian_watch import Patience, Silence, StoppedAnswering
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.blocks import BlockRegistry, TriggerContext
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.testing import fake_plugin
from spanreed.workflows.values import from_data

START = datetime.datetime(2026, 11, 24, 8, 0, tzinfo=datetime.timezone.utc)
BEAT = json.dumps({"kind": "watchdog"})
ERROR = {"kind": "error", "message": "Vault is locked"}


class _Clock:
    """A clock that tests move, and that a sleep moves on."""

    def __init__(self) -> None:
        self.now = START
        self.slept: list[float] = []

    def after(self, minutes: float) -> datetime.datetime:
        self.now += datetime.timedelta(minutes=minutes)
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += datetime.timedelta(seconds=seconds)
        # Let what waits for the clock run.
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> Iterator[_Clock]:
    obsidian_watch.reset_for_tests()
    clock = _Clock()
    with patch("spanreed.apis.obsidian_watch._now", lambda: clock.now), patch(
        "spanreed.apis.obsidian_watch._sleep", clock.sleep
    ):
        yield clock
    obsidian_watch.reset_for_tests()


class _State:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


def _ctx(state: _State | None = None, timezone: str = "UTC") -> TriggerContext:
    user = mock_user_find_by_id(4)
    user.tzinfo = dateutil.tz.gettz(timezone)
    return TriggerContext(
        user=user,
        workflow_id="wf_1",
        state=state or _State(),
        queue=WorkflowQueue("wf_1"),
    )


# -- what the plugin says ----------------------------------------------------


def _take(clock: _Clock, *said: tuple[float, Any]) -> list[tuple[float, str]]:
    """Take in each thing after its minutes. Return what was reported, and when."""
    reported: list[tuple[float, str]] = []

    async def report(text: str) -> None:
        reported.append(((clock.now - START).total_seconds() / 60, text))

    async def run() -> None:
        obsidian_watch.vault_of(4)
        for minutes, thing in said:
            clock.after(minutes)
            await obsidian_watch.take(4, thing)

    with patch("spanreed.apis.obsidian_watch.errors.report", new=report):
        asyncio.run(run())
    return reported


def test_a_heartbeat_is_a_sign_of_life(clock: _Clock) -> None:
    assert obsidian_watch.vault_of(4).last_seen is None
    assert obsidian_watch.vault_of(4).quiet_since == START

    assert _take(clock, (2, BEAT)) == []

    vault = obsidian_watch.vault_of(4)
    assert vault.last_seen == START + datetime.timedelta(minutes=2)
    assert vault.quiet_since == vault.last_seen


def test_an_error_is_reported_at_most_once_in_the_pause(clock: _Clock) -> None:
    error = json.dumps(ERROR)

    reported = _take(clock, (31, error), (10, error), (30, error))

    text = "Obsidian plugin error: {'kind': 'error', 'message': 'Vault is locked'}"
    assert reported == [(31, text), (71, text)]


def test_no_error_is_reported_in_the_first_pause_after_the_start(
    clock: _Clock,
) -> None:
    assert _take(clock, (10, json.dumps(ERROR)), (20, json.dumps(ERROR))) == []


def test_the_errors_of_a_dropped_connection_are_left_out(clock: _Clock) -> None:
    said = [
        (31 if place == 0 else 1, json.dumps({"kind": "error", "message": message}))
        for place, message in enumerate(obsidian_watch.IGNORED_ERRORS)
    ]

    assert _take(clock, *said) == []
    # They did not start a pause either.
    assert len(_take(clock, (1, json.dumps(ERROR)))) == 1


@pytest.mark.parametrize(
    "raw", [b"not json", "[1, 2]", json.dumps({"kind": "other"}), None, "{}"]
)
def test_what_is_not_understood_is_passed_over(clock: _Clock, raw: Any) -> None:
    assert _take(clock, (45, raw)) == []
    assert obsidian_watch.vault_of(4).last_seen is None


def test_an_error_is_read_from_bytes_too(clock: _Clock) -> None:
    reported = _take(clock, (31, json.dumps(ERROR).encode("utf-8")))

    assert [minutes for minutes, _ in reported] == [31]


# -- reading -----------------------------------------------------------------


def test_reading_takes_in_what_the_plugin_says_and_goes_on_after_a_failure(
    clock: _Clock,
) -> None:
    said: list[Any] = [BEAT, ConnectionError("Redis is away"), None, BEAT]
    asked: list[int] = []

    async def next_said(user_id: int) -> Any:
        asked.append(user_id)
        if not said:
            raise asyncio.CancelledError()
        thing = said.pop(0)
        if isinstance(thing, Exception):
            raise thing
        clock.after(1)
        return thing

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await obsidian_watch._read(4)

    with patch("spanreed.apis.obsidian_watch._next", new=next_said):
        asyncio.run(run())

    assert asked == [4] * 5
    # A minute after the failure, a second after nothing came.
    assert clock.slept == [obsidian_watch.RETRY_S, 1]
    assert obsidian_watch.vault_of(4).last_seen == clock.now


def test_the_list_of_a_user_is_read_once(clock: _Clock) -> None:
    started: list[int] = []

    async def read(user_id: int) -> None:
        started.append(user_id)
        await asyncio.Event().wait()

    async def run() -> None:
        Plugin.reset_registry()
        plugin = ObsidianPlugin()
        user = mock_user_find_by_id(4)
        # The plugin starts the reading for a user, and returns.
        await plugin._watch(user)
        obsidian_watch.start_reading(user)
        obsidian_watch.start_reading(mock_user_find_by_id(9))
        await asyncio.sleep(0)

    with patch("spanreed.apis.obsidian_watch._read", new=read):
        asyncio.run(run())

    assert started == [4, 9]


def test_the_plugin_reads_for_each_of_its_users() -> None:
    Plugin.reset_registry()
    plugin = ObsidianPlugin()

    async def serve_users(served: Any, job: Any) -> None:
        assert (served, job) == (plugin, plugin._watch)

    with patch("spanreed.apis.obsidian.serve_users", new=serve_users):
        asyncio.run(plugin.run())


def test_next_waits_on_the_list_of_the_user() -> None:
    redis = AsyncMock()
    redis.blpop.return_value = (b"obsidian-plugin-monitor:4", b'{"kind": "watchdog"}')

    with patch("spanreed.apis.obsidian_watch.redis_api", new=redis):
        assert asyncio.run(obsidian_watch._next(4)) == b'{"kind": "watchdog"}'

    redis.blpop.assert_awaited_once_with("obsidian-plugin-monitor:4", timeout=60)


def test_next_gives_nothing_when_the_wait_is_over() -> None:
    redis = AsyncMock()
    redis.blpop.return_value = None

    with patch("spanreed.apis.obsidian_watch.redis_api", new=redis):
        assert asyncio.run(obsidian_watch._next(4)) is None


# -- Vault stopped answering -------------------------------------------------


def test_the_trigger_is_well_formed() -> None:
    registry = BlockRegistry()
    registry.register(StoppedAnswering(), fake_plugin())

    assert StoppedAnswering.Manual is None
    assert from_data(Patience, {}) == Patience(after=1, at_most_every=30)
    for params in ({"after": 0}, {"at_most_every": 0}):
        with pytest.raises(ValueError):
            from_data(Patience, params)


async def _events(
    clock: _Clock,
    ctx: TriggerContext,
    params: Patience,
    count: int,
    beats_at: tuple[float, ...] = (),
) -> list[tuple[float, Silence]]:
    """Return the first events of the trigger, each with the minute it came at.

    ``beats_at`` are the minutes, from the start, at which a heartbeat
    comes.
    """
    beats = [START + datetime.timedelta(minutes=minutes) for minutes in beats_at]

    async def sleep(seconds: float) -> None:
        # The heartbeats that come while the trigger waits.
        until = clock.now + datetime.timedelta(seconds=seconds)
        while beats and beats[0] <= until:
            clock.now = beats.pop(0)
            await obsidian_watch.take(4, BEAT)
        clock.now = until
        clock.slept.append(seconds)
        await asyncio.sleep(0)

    found = []
    with patch("spanreed.apis.obsidian_watch._sleep", sleep), patch(
        "spanreed.apis.obsidian_watch.start_reading"
    ) as start_reading:
        events = StoppedAnswering().events(ctx, params)
        async for event in events:
            found.append(((clock.now - START).total_seconds() / 60, event))
            if len(found) == count:
                break
        await events.aclose()
    # The trigger sees to it that the list of its user is read.
    start_reading.assert_called_once_with(ctx.user)
    return found


def test_it_fires_after_the_quiet_minutes_and_then_at_most_every_so_often(
    clock: _Clock,
) -> None:
    found = asyncio.run(_events(clock, _ctx(), Patience(after=2, at_most_every=30), 3))

    # No sign of life since Spanreed started: there is no last time.
    assert found == [
        (2, Silence(last_seen="", minutes=2)),
        (32, Silence(last_seen="", minutes=32)),
        (62, Silence(last_seen="", minutes=62)),
    ]


def test_it_does_not_fire_while_the_heartbeats_come(clock: _Clock) -> None:
    beats = tuple(minute / 2 for minute in range(1, 241))  # two hours of them

    found = asyncio.run(
        _events(
            clock,
            _ctx(timezone="Asia/Jerusalem"),
            Patience(after=1, at_most_every=30),
            1,
            beats_at=beats,
        )
    )

    # A minute after the last one, at 10:00 UTC.
    assert found == [(121, Silence(last_seen="2026-11-24 12:00", minutes=1))]


def test_a_vault_that_comes_and_goes_does_not_fire_more_often(clock: _Clock) -> None:
    found = asyncio.run(
        _events(
            clock,
            _ctx(),
            Patience(after=1, at_most_every=30),
            2,
            # Quiet from the start, back at minute 5, and quiet again.
            beats_at=(5,),
        )
    )

    assert [minute for minute, _ in found] == [1, 31]
    assert found[1][1] == Silence(last_seen="2026-11-24 08:05", minutes=26)


def test_a_restart_does_not_fire_again_before_its_time(clock: _Clock) -> None:
    state = _State()
    # It fired ten minutes before Spanreed started again.
    state.values["fired_at"] = (START - datetime.timedelta(minutes=10)).isoformat()

    found = asyncio.run(_events(clock, _ctx(state), Patience(), 1))

    assert [minute for minute, _ in found] == [20]
    assert state.values["fired_at"] == clock.now.isoformat()


def test_a_time_that_cannot_be_read_is_no_time(clock: _Clock) -> None:
    state = _State()
    state.values["fired_at"] = "long ago"

    found = asyncio.run(_events(clock, _ctx(state), Patience(), 1))

    assert [minute for minute, _ in found] == [1]
