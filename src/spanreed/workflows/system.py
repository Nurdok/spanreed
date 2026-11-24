"""Triggers for what happens to Spanreed itself.

``Started`` fires when Spanreed starts. ``ErrorReported`` fires for an
error that Spanreed kept (see ``spanreed.errors``): a command that crashed,
a token that could not be refreshed, the error that ended the process.

They are what the Spanreed Monitor plugin told its users about. Both are
fed by the plugin that runs the engine: ``say_started`` once it has
started, and ``pass_errors_on`` for as long as it runs.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from spanreed import errors
from spanreed.user import User
from spanreed.workflows.blocks import Block, NoParams, PushTrigger, TriggerContext

_logger = logging.getLogger(__name__)

# How long ``pass_errors_on`` waits after it failed itself, for example
# because Redis was away.
RETRY_S = 60.0

# Indirection so that tests can skip the wait.
_sleep = asyncio.sleep


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock.
    return datetime.datetime.now(datetime.timezone.utc)


def _local(ctx: TriggerContext, at: str) -> tuple[str, str] | None:
    """Return the date and the time of day of an instant, for the user."""
    try:
        instant = datetime.datetime.fromisoformat(at)
    except ValueError:
        return None
    local = instant.astimezone(ctx.user.tzinfo)
    return local.date().isoformat(), local.strftime("%H:%M")


@dataclass
class Start:
    """The event of ``Started``: when Spanreed started, in the user's timezone."""

    # YYYY-MM-DD
    date: str
    # HH:MM
    time: str


class Started(PushTrigger[NoParams, Start]):
    """Fire each time Spanreed has started.

    A workflow that was on when Spanreed started gets the event, also if
    it could not run then: the event waits. A workflow that is turned on
    later waits for the next start.

    The key of a start is its time.
    """

    id = "core.started"
    title = "Spanreed started"

    async def event_for(
        self, ctx: TriggerContext, params: NoParams, key: str
    ) -> Start | None:
        """Return when Spanreed started, or ``None`` for a key that is no time."""
        local = _local(ctx, key)
        return None if local is None else Start(date=local[0], time=local[1])


@dataclass
class Trouble:
    """The event of ``ErrorReported``."""

    # What went wrong: the error, with the last lines of where it came from.
    text: str
    # When it was passed on, in the user's timezone: YYYY-MM-DD and HH:MM.
    date: str
    time: str


class ErrorReported(PushTrigger[NoParams, Trouble]):
    """Fire for an error that Spanreed kept.

    The errors are those of Spanreed as a whole, not of one user: every
    workflow with this trigger gets each of them. The failed run of a
    workflow is not one of them. The engine tells its user about that
    itself.

    The key of an error holds its text and its time.
    """

    id = "core.error"
    title = "Spanreed had an error"

    @staticmethod
    def key(text: str, at: datetime.datetime) -> str:
        """Return the key of an error that was passed on at ``at``."""
        return json.dumps({"at": at.isoformat(), "text": text})

    async def event_for(
        self, ctx: TriggerContext, params: NoParams, key: str
    ) -> Trouble | None:
        """Return the error of a key, or ``None`` for a key that holds none."""
        try:
            kept = json.loads(key)
        except ValueError:
            return None
        if not isinstance(kept, dict) or not isinstance(kept.get("text"), str):
            return None
        local = _local(ctx, str(kept.get("at")))
        if local is None:
            return None
        return Trouble(text=kept["text"], date=local[0], time=local[1])


async def say_started(users: Iterable[User]) -> None:
    """Fire ``Started`` for these users."""
    key = _now().isoformat()
    for user in users:
        await Started.emit(user, [key])


async def pass_errors_on(users: Callable[[], Awaitable[list[User]]]) -> None:
    """Fire ``ErrorReported`` for each error that Spanreed keeps. Never return.

    ``users`` returns the users to fire it for, when an error is there.

    An error rarely comes alone, so the ones that wait behind the newest
    are dropped: they are in the log. An error that no workflow wants is
    dropped as well.
    """
    while True:
        try:
            text = await errors.next_report()
            await errors.drop_waiting()
            key = ErrorReported.key(text, _now())
            for user in await users():
                await ErrorReported.emit(user, [key])
        except Exception:
            # Not reported as an error itself: that would come back here.
            _logger.exception(
                f"Could not pass an error on. Trying again in {RETRY_S:.0f}s."
            )
            await _sleep(RETRY_S)


def system_blocks() -> list[Block]:
    """Return the blocks for what happens to Spanreed itself."""
    return [Started(), ErrorReported()]
