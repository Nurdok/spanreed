"""The errors that Spanreed keeps for someone to look at.

What goes wrong outside the run of a workflow, such as a command that
crashes, has nobody to tell: it is written to the log, and its text is put
in a list in Redis. The trigger "Spanreed had an error" hands what is in
the list to the workflows that want to know (see
``spanreed.workflows.system``).

The list is in Redis so that the error that ends the process is there when
Spanreed starts again.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from spanreed.storage import redis_api

# The name is that of the plugin that read the list before workflows did.
# It is kept, so that what waited in the list is not lost.
QUEUE = "spanreed-monitor-exceptions"
# The seconds that one wait for an error takes, before the next one starts.
WAIT_S = 60


async def report(text: str) -> None:
    """Keep the text of an error for someone to look at."""
    await redis_api.lpush(QUEUE, text)


async def next_report() -> str:
    """Wait for an error, and return its text. The newest one comes first."""
    while True:
        # Not one wait without an end: a connection that is quiet for
        # hours gets cut on the way.
        found = await redis_api.blpop(QUEUE, timeout=WAIT_S)
        if found is not None:
            text = found[1]
            return text.decode("utf-8") if isinstance(text, bytes) else str(text)
        # Redis has waited. What stands in for it in a test has not.
        await asyncio.sleep(0)


async def drop_waiting() -> None:
    """Drop the errors that wait.

    An error rarely comes alone. Who looks at one of them does not need
    the hundred that came with it: those are in the log.
    """
    await redis_api.delete(QUEUE)


class BoolValue:
    """A yes or a no that is set once, and cannot be read before."""

    def __init__(self) -> None:
        """Create the value, not set yet."""
        self.value: bool | None = None

    def set(self, value: bool) -> None:
        """Set the value. Raise ``RuntimeError`` if it is set already."""
        if self.value is not None:
            raise RuntimeError("Value already set.")
        self.value = value

    def __bool__(self) -> bool:
        """Return the value. Raise ``RuntimeError`` if it is not set."""
        if self.value is None:
            raise RuntimeError("Value not set.")
        return self.value


@asynccontextmanager
async def suppress_and_log_exception(
    *exceptions: type[BaseException],
) -> AsyncGenerator[BoolValue, None]:
    """Keep an error of these types from going further, and report it.

    What is yielded says, after the block, whether an error was kept back.
    """
    logger = logging.getLogger(__name__)
    did_suppress = BoolValue()
    try:
        yield did_suppress
    except Exception as e:
        if not any(isinstance(e, exception) for exception in exceptions):
            did_suppress.set(False)
            raise
        did_suppress.set(True)
        exception_str = "".join(traceback.format_exception(type(e), e, None, limit=3))
        logger.exception("Suppressed exception")
        await report(exception_str)
    else:
        did_suppress.set(False)
