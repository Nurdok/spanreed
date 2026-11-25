"""What the Spanreed plugin for Obsidian says about itself.

While Obsidian is open, its plugin puts two things in a list in Redis, one
list for each user: a heartbeat, again and again, and each error it had.
``start_reading`` reads the list of a user:

- The time of the last heartbeat is kept, for the trigger "Vault stopped
  answering".
- An error is reported as one of Spanreed's errors (see
  ``spanreed.errors``), at most one in ``ERROR_PAUSE``. The errors of a
  connection that dropped are left out: the plugin gets over those itself.

The Spanreed Monitor plugin read the list before, for its own users only.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from spanreed import errors
from spanreed.config_form import HELP_KEY, described
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.workflows.blocks import Trigger, TriggerContext

_logger = logging.getLogger(__name__)

QUEUE = "obsidian-plugin-monitor"
# What the plugin says when its connection to Redis dropped.
IGNORED_ERRORS = (
    "read ECONNRESET",
    "read ETIMEDOUT",
    "Socket closed unexpectedly",
)
# No error is reported this long after the last one, or after the start.
ERROR_PAUSE = datetime.timedelta(minutes=30)
# How long the reading waits after it failed itself.
RETRY_S = 60.0
# The seconds that one wait for the plugin takes, before the next starts.
WAIT_S = 60

_FIRED_AT = "fired_at"

# Indirection so that tests can skip the waits.
_sleep = asyncio.sleep


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock.
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class Vault:
    """What this process knows about the plugin of one user."""

    # When the reading started: nothing is known from before.
    since: datetime.datetime
    # When the last heartbeat came, if one did.
    last_seen: datetime.datetime | None = None
    # When an error was last reported, or the reading started.
    error_at: datetime.datetime = field(init=False)

    def __post_init__(self) -> None:
        """Hold errors back at first, as after one that was reported."""
        self.error_at = self.since

    @property
    def quiet_since(self) -> datetime.datetime:
        """Return when the plugin was last known to run."""
        return self.last_seen or self.since


_vaults: dict[int, Vault] = {}
_readers: dict[int, asyncio.Task[None]] = {}


def vault_of(user_id: int) -> Vault:
    """Return what is known about the plugin of a user."""
    if user_id not in _vaults:
        _vaults[user_id] = Vault(since=_now())
    return _vaults[user_id]


def reset_for_tests() -> None:
    """Forget every vault, and the tasks that read for them."""
    _readers.clear()
    _vaults.clear()


def start_reading(user: User) -> None:
    """Read what the plugin of a user says, unless that is being done.

    The reading goes on until Spanreed stops.
    """
    task = _readers.get(user.id)
    if task is None or task.done():
        vault_of(user.id)
        _readers[user.id] = asyncio.create_task(
            _read(user.id), name=f"obsidian-watch-{user.id}"
        )


async def _next(user_id: int) -> Any:
    """Wait for the next thing the plugin of a user said, and return it.

    Return ``None`` if nothing came in ``WAIT_S``. The wait has an end
    because a connection that is quiet for hours, as while Obsidian is
    closed, gets cut on the way.
    """
    found = await redis_api.blpop(f"{QUEUE}:{user_id}", timeout=WAIT_S)
    return None if found is None else found[1]


async def _read(user_id: int) -> None:
    while True:
        try:
            raw = await _next(user_id)
            if raw is None:
                # Redis has waited. What stands in for it in a test has
                # not: do not spin.
                await _sleep(1)
                continue
            await take(user_id, raw)
        except Exception:
            _logger.exception(
                f"Could not read what the Obsidian plugin of user {user_id}"
                f" says. Trying again in {RETRY_S:.0f}s."
            )
            await _sleep(RETRY_S)


async def take(user_id: int, raw: Any) -> None:
    """Take in one thing that the plugin of a user said."""
    try:
        said = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(said, dict):
        return
    vault = vault_of(user_id)
    if said.get("kind") == "watchdog":
        vault.last_seen = _now()
    elif said.get("kind") == "error":
        if said.get("message") in IGNORED_ERRORS:
            _logger.info(
                f"The Obsidian plugin of user {user_id} lost its connection:"
                f" {said['message']}"
            )
            return
        # What the error says may name a note, so it stays out of the log.
        # It reaches its user as an error of Spanreed.
        _logger.info(f"The Obsidian plugin of user {user_id} had an error.")
        if _now() - vault.error_at > ERROR_PAUSE:
            await errors.report(f"Obsidian plugin error: {said}")
            vault.error_at = _now()


@dataclass
class Patience:
    """How long a vault may be quiet, and how often to say that it is."""

    after: int = field(
        default=1,
        metadata={HELP_KEY: "Minutes without a sign of life from Obsidian"},
    )
    at_most_every: int = field(
        default=30,
        metadata={
            HELP_KEY: "Minutes before it fires again, while Obsidian stays quiet"
        },
    )

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for waits that mean nothing."""
        if self.after < 1 or self.at_most_every < 1:
            raise ValueError("Give 1 minute or more.")


@dataclass
class Silence:
    """The event of ``StoppedAnswering``."""

    last_seen: str = described(
        "When Obsidian last gave a sign of life, as 2026-05-09 18:30, in "
        "your timezone. Empty if it gave none since Spanreed started",
    )
    minutes: int = described(
        "The whole minutes since then, or since Spanreed started",
    )


class StoppedAnswering(Trigger[Patience, Silence]):
    """Fire when Obsidian gave no sign of life for a while.

    Obsidian's plugin for Spanreed says that it runs, again and again,
    while Obsidian is open. The trigger fires when that stops for ``after``
    minutes, and again every ``at_most_every`` minutes for as long as it
    stays quiet. It does not fire more often than that either when
    Obsidian comes and goes.

    Obsidian is quiet whenever it is closed, so this is for a vault that
    is meant to be open all the time.
    """

    id = "obsidian.stopped-answering"
    title = "Vault stopped answering"

    async def events(
        self, ctx: TriggerContext, params: Patience
    ) -> AsyncGenerator[Silence, bool | None]:
        """Yield an event each time the vault was quiet for long enough."""
        start_reading(ctx.user)
        vault = vault_of(ctx.user.id)
        after = datetime.timedelta(minutes=params.after)
        pause = datetime.timedelta(minutes=params.at_most_every)
        while True:
            due = vault.quiet_since + after
            fired_at = await self._fired_at(ctx)
            if fired_at is not None:
                due = max(due, fired_at + pause)
            wait = (due - _now()).total_seconds()
            if wait > 0:
                # A heartbeat may come in the meantime: look again then.
                await _sleep(wait)
                continue
            now = _now()
            # Kept before the run, so that a run that fails, and is tried
            # again for an hour, is not followed by the next one at once.
            await ctx.state.set(_FIRED_AT, now.isoformat())
            yield Silence(
                last_seen=(
                    vault.last_seen.astimezone(ctx.user.tzinfo).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if vault.last_seen is not None
                    else ""
                ),
                minutes=int((now - vault.quiet_since).total_seconds() // 60),
            )

    @staticmethod
    async def _fired_at(ctx: TriggerContext) -> datetime.datetime | None:
        stored = await ctx.state.get(_FIRED_AT)
        if stored is None:
            return None
        try:
            return datetime.datetime.fromisoformat(stored)
        except ValueError:
            return None
