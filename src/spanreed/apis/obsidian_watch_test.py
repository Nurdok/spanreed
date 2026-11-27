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
from spanreed.apis.obsidian_watch import (
    Absence,
    AnsweringAgain,
    Patience,
    Return,
    Silence,
    StoppedAnswering,
)
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


def test_what_an_error_says_stays_out_of_the_log(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    secret = {"kind": "error", "message": "Cannot read Journal/Therapy.md"}
    dropped = {"kind": "error", "message": "read ECONNRESET"}

    with caplog.at_level("INFO", logger="spanreed.apis.obsidian_watch"):
        reported = _take(clock, (31, json.dumps(secret)), (1, json.dumps(dropped)))

    # The user is told what it said. The log only says that there was one.
    assert "Therapy" in reported[0][1]
    assert "Therapy" not in caplog.text
    assert "The Obsidian plugin of user 4 had an error." in caplog.text
    # A dropped connection says nothing about the vault.
    assert "lost its connection: read ECONNRESET" in caplog.text


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


def test_the_triggers_are_well_formed() -> None:
    registry = BlockRegistry()
    registry.register(StoppedAnswering(), fake_plugin())
    registry.register(AnsweringAgain(), fake_plugin())

    assert StoppedAnswering.Manual is None
    assert AnsweringAgain.Manual is None
    assert from_data(Patience, {}) == Patience(after=1, at_most_every=30, remind=False)
    assert from_data(Absence, {}) == Absence(after=1)
    for params in ({"after": 0}, {"at_most_every": 0}):
        with pytest.raises(ValueError):
            from_data(Patience, params)
    with pytest.raises(ValueError):
        from_data(Absence, {"after": 0})


class _GaveUp(Exception):
    """The trigger waited longer than a test lets it."""


async def _events(
    clock: _Clock,
    ctx: TriggerContext,
    params: Any,
    count: int,
    beats_at: tuple[float, ...] = (),
    trigger: Any = None,
    until: float = 24 * 60,
) -> list[tuple[float, Any]]:
    """Return the first events of a trigger, each with the minute it came at.

    ``beats_at`` are the minutes, from the start, at which a heartbeat
    comes. The events are those of "Vault stopped answering" unless
    another ``trigger`` is given. No event after minute ``until`` is
    waited for.
    """
    beats = [START + datetime.timedelta(minutes=minutes) for minutes in beats_at]
    end = START + datetime.timedelta(minutes=until)

    async def sleep(seconds: float) -> None:
        # The heartbeats that come while the trigger waits.
        wake = clock.now + datetime.timedelta(seconds=seconds)
        while beats and beats[0] <= wake:
            clock.now = beats.pop(0)
            await obsidian_watch.take(4, BEAT)
        clock.now = wake
        clock.slept.append(seconds)
        if clock.now > end:
            raise _GaveUp()
        await asyncio.sleep(0)

    found = []
    with patch("spanreed.apis.obsidian_watch._sleep", sleep), patch(
        "spanreed.apis.obsidian_watch.start_reading"
    ) as start_reading:
        events = (trigger or StoppedAnswering()).events(ctx, params)
        try:
            async for event in events:
                found.append(((clock.now - START).total_seconds() / 60, event))
                if len(found) == count:
                    break
        except _GaveUp:
            pass
        await events.aclose()
    # The trigger sees to it that the list of its user is read.
    start_reading.assert_called_once_with(ctx.user)
    return found


def test_it_fires_once_for_a_silence_however_long_it_lasts(clock: _Clock) -> None:
    found = asyncio.run(_events(clock, _ctx(), Patience(after=2), 3, until=600))

    # Two minutes on top of the two that heartbeats may be apart. No sign
    # of life since Spanreed started: there is no last time.
    assert found == [(4, Silence(last_seen="", minutes=4))]


def test_it_can_fire_again_and_again_while_the_vault_stays_quiet(
    clock: _Clock,
) -> None:
    found = asyncio.run(
        _events(clock, _ctx(), Patience(after=2, at_most_every=30, remind=True), 3)
    )

    assert found == [
        (4, Silence(last_seen="", minutes=4)),
        (34, Silence(last_seen="", minutes=34)),
        (64, Silence(last_seen="", minutes=64)),
    ]


def test_it_does_not_fire_while_the_heartbeats_come(clock: _Clock) -> None:
    # Two hours of them, a little more than a minute apart, as the plugin
    # sends them.
    beats = tuple(minute * 1.01 for minute in range(1, 120))

    found = asyncio.run(
        _events(
            clock,
            _ctx(timezone="Asia/Jerusalem"),
            Patience(after=1),
            1,
            beats_at=beats,
        )
    )

    # Three minutes after the last one, which came at 10:00 UTC.
    assert [round(minute, 2) for minute, _ in found] == [round(119 * 1.01 + 3, 2)]
    assert found[0][1] == Silence(last_seen="2026-11-24 12:00", minutes=3)


def test_it_fires_again_for_the_next_silence(clock: _Clock) -> None:
    found = asyncio.run(
        _events(
            clock,
            _ctx(),
            Patience(after=1, at_most_every=30),
            2,
            # Quiet from the start, back at minute 40, and quiet again.
            beats_at=(40,),
        )
    )

    assert [minute for minute, _ in found] == [3, 43]
    assert found[1][1] == Silence(last_seen="2026-11-24 08:40", minutes=3)


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

    assert [minute for minute, _ in found] == [3, 33]
    assert found[1][1] == Silence(last_seen="2026-11-24 08:05", minutes=28)


def test_a_restart_in_a_silence_that_was_told_does_not_fire_again(
    clock: _Clock,
) -> None:
    state = _State()
    # It fired two hours before Spanreed started again, and nothing was
    # heard since.
    state.values["fired_at"] = (START - datetime.timedelta(hours=2)).isoformat()
    state.values["quiet"] = "1"

    found = asyncio.run(
        _events(clock, _ctx(state), Patience(), 2, beats_at=(50,), until=600)
    )

    # Only the silence after the vault answered, at minute 50.
    assert [minute for minute, _ in found] == [53]


def test_a_restart_does_not_fire_again_before_its_time(clock: _Clock) -> None:
    state = _State()
    # It fired ten minutes before Spanreed started again, for a silence
    # that is over.
    state.values["fired_at"] = (START - datetime.timedelta(minutes=10)).isoformat()

    found = asyncio.run(_events(clock, _ctx(state), Patience(), 1))

    assert [minute for minute, _ in found] == [20]
    assert state.values["fired_at"] == clock.now.isoformat()


def test_a_time_that_cannot_be_read_is_no_time(clock: _Clock) -> None:
    state = _State()
    state.values["fired_at"] = "long ago"

    found = asyncio.run(_events(clock, _ctx(state), Patience(), 1))

    assert [minute for minute, _ in found] == [3]


# -- the vault answers again ---------------------------------------------------


def _returns(
    clock: _Clock, state: _State | None = None, **how: Any
) -> list[tuple[float, Return]]:
    how.setdefault("until", 600)
    return asyncio.run(
        _events(
            clock,
            _ctx(state, timezone="Asia/Jerusalem"),
            Absence(after=how.pop("after", 1)),
            how.pop("count", 5),
            trigger=AnsweringAgain(),
            **how,
        )
    )


def test_it_fires_when_a_quiet_vault_answers(clock: _Clock) -> None:
    # Up for ten minutes, quiet for fifty, and up again.
    beats = (*range(1, 11), *range(60, 70))

    found = _returns(clock, beats_at=beats, until=120)

    assert found == [(60, Return(quiet_since="2026-11-24 10:10", minutes=50))]


def test_a_vault_that_answers_all_along_did_not_come_back(clock: _Clock) -> None:
    assert _returns(clock, beats_at=tuple(range(1, 200)), until=180) == []


def test_a_gap_shorter_than_the_wait_is_no_return(clock: _Clock) -> None:
    # Five minutes between two signs, where only more than seven count.
    assert _returns(clock, after=5, beats_at=(1, 2, 7, 8), until=8.5) == []


def test_it_fires_for_each_return(clock: _Clock) -> None:
    found = _returns(clock, beats_at=(1, 30, 31, 90), until=100)

    assert [(minute, event.minutes) for minute, event in found] == [(30, 29), (90, 59)]


def test_a_vault_that_was_quiet_before_a_restart_fires_when_it_answers(
    clock: _Clock,
) -> None:
    state = _State()
    # Quiet since three hours before Spanreed started again.
    state.values["quiet_from"] = (START - datetime.timedelta(hours=3)).isoformat()

    found = _returns(clock, state, beats_at=tuple(range(1, 12)), until=10)

    assert found == [(1, Return(quiet_since="2026-11-24 07:00", minutes=181))]
    assert state.values["quiet_from"] == ""
