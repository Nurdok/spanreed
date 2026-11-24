"""Keep a job running for each user of a plugin.

A plugin does nothing by itself any more: what plugins did for their users
are workflows now. What is left is a plugin that is the connection to a
service and has to listen to it. Health Connect takes what the phone sent
out of an inbox, and Obsidian reads what its own plugin says. Such a plugin
calls ``serve_users`` from its ``run``.

The users are read again every ``LOOK_EVERY_S``, so a user who registers to
the plugin is served without a restart, and one who leaves it is no longer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from spanreed.user import User

if TYPE_CHECKING:
    from spanreed.plugin import Plugin

_logger = logging.getLogger(__name__)

# A job that failed starts again after a pause: the first after this many
# seconds, then twice as long each time, up to the second number. A job
# that ran longer than the third before it failed starts its pauses over.
#
# The pause must grow. A job that fails for hours, because the service is
# away, must not turn into a tight loop: on 2026-07-21 a plugin that
# crashed on every try was restarted 1,100 times in a day.
RESTART_INITIAL_S = 60.0
RESTART_MAX_S = 1800.0
RESTART_RESET_UPTIME_S = 600.0
# How often the users of the plugin are read again.
LOOK_EVERY_S = 60.0

# Indirections so that tests can skip the waits and set the clock without
# patching asyncio.sleep and time.monotonic, which asyncio uses itself.
_sleep = asyncio.sleep
_monotonic = time.monotonic


async def serve_users(plugin: Plugin, serve: Callable[[User], Awaitable[None]]) -> None:
    """Run ``serve`` for each user of a plugin, in a task of its own. Never return.

    A job that returns is done: it is not started again while its user
    stays registered. A job that fails is started again after a pause. The
    job of a user who is no longer registered is cancelled, and so is
    every job when this is.
    """
    jobs: dict[int, asyncio.Task[None]] = {}
    try:
        while True:
            try:
                await _match(plugin, serve, jobs)
            except Exception:
                # The jobs that run go on. The next look may work.
                _logger.exception(
                    f"Could not read the users of {plugin.canonical_name()}."
                )
            await _sleep(LOOK_EVERY_S)
    finally:
        for job in jobs.values():
            job.cancel()
        await asyncio.gather(*jobs.values(), return_exceptions=True)


async def _match(
    plugin: Plugin,
    serve: Callable[[User], Awaitable[None]],
    jobs: dict[int, asyncio.Task[None]],
) -> None:
    """Make the jobs that run match the users the plugin has now."""
    user_ids = await plugin.user_ids()
    for user_id in set(jobs) - user_ids:
        jobs.pop(user_id).cancel()
    for user_id in sorted(user_ids - set(jobs)):
        user = await User.find_by_id(user_id)
        _logger.info(f"Serving user {user_id} of {plugin.canonical_name()}.")
        jobs[user_id] = asyncio.create_task(
            _keep_serving(plugin, serve, user),
            name=f"{plugin.canonical_name()}-{user_id}",
        )


async def _keep_serving(
    plugin: Plugin, serve: Callable[[User], Awaitable[None]], user: User
) -> None:
    """Run the job of one user, again after a pause each time it fails."""
    delay = RESTART_INITIAL_S
    while True:
        started = _monotonic()
        try:
            await serve(user)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            if _monotonic() - started > RESTART_RESET_UPTIME_S:
                delay = RESTART_INITIAL_S
            # The words that deploy.sh looks for in the log of a start.
            _logger.exception(
                f"{plugin.canonical_name()} crashed for user {user.id};"
                f" restarting in {delay:.0f}s."
            )
            await _sleep(delay)
            delay = min(delay * 2, RESTART_MAX_S)
