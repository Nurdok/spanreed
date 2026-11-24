"""Keep workflows, their triggers' state and their runs' traces in Redis.

Keys:

- ``workflows:user_id={id}``            set of the user's workflow ids
- ``workflows:workflow:{id}``           the workflow, as JSON
- ``workflows:workflow:{id}:state``     hash of what its trigger remembers
- ``workflows:workflow:{id}:memory``    hash of what its steps remember, each
  value as JSON with the time it was kept
- ``workflows:workflow:{id}:runs``      list of run traces, newest first
- ``workflows:workflow:{id}:queue``     list of the keys it still has to run
  for, oldest first
- ``workflows:workflow:{id}:queued``    hash of those keys, each with a
  token that changes when the key is put again

A workflow keeps the traces of its last ``RUNS_KEPT`` runs. They are dropped
``RUNS_TTL`` after the last run.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import secrets
from collections.abc import Iterable
from typing import Any

from spanreed.storage import redis_api
from spanreed.workflows.blocks import Remembered
from spanreed.workflows.model import Workflow

RUNS_KEPT = 100
# The name under which a workflow that took the place of a plugin remembers
# that it was given what the plugin knew. It is the engine's own note, so
# it is not shown with what the workflow remembers.
MOVED_MARKER = "moved-from-plugin"
RUNS_TTL = datetime.timedelta(days=30)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _user_key(user_id: int) -> str:
    return f"workflows:user_id={user_id}"


def _workflow_key(workflow_id: str) -> str:
    return f"workflows:workflow:{workflow_id}"


class StoredTriggerState:
    """What the trigger of one workflow remembers between events."""

    def __init__(self, workflow_id: str) -> None:
        """Create the state of the workflow with this id."""
        self._key = f"{_workflow_key(workflow_id)}:state"

    async def get(self, name: str) -> str | None:
        """Return a stored value, or ``None`` if it is not set."""
        value = await redis_api.hget(self._key, name)
        return None if value is None else _text(value)

    async def set(self, name: str, value: str) -> None:
        """Store a value."""
        await redis_api.hset(self._key, name, value)


# Spanreed is one process, so a lock and an event here are enough to keep
# the two Redis keys of a queue in step and to wake the workflow that waits
# for a key.
_queue_locks: dict[str, asyncio.Lock] = {}
_queue_wakeups: dict[str, asyncio.Event] = {}


def reset_queues_for_tests() -> None:
    """Forget the locks and wake-ups, which belong to an event loop."""
    _queue_locks.clear()
    _queue_wakeups.clear()


class StoredMemory:
    """What the steps of one workflow remember from one run to the next."""

    def __init__(self, workflow_id: str) -> None:
        """Create the memory of the workflow with this id."""
        self._key = f"{_workflow_key(workflow_id)}:memory"

    async def get(self, name: str) -> Remembered | None:
        """Return what is kept under a name, or ``None`` if nothing is."""
        raw = await redis_api.hget(self._key, name)
        if raw is None:
            return None
        kept = json.loads(_text(raw))
        return Remembered(value=kept["value"], at=kept["at"])

    async def set(self, name: str, value: Any = True, at: str | None = None) -> None:
        """Keep a JSON value under a name, in place of what was there.

        ``at`` is when it was kept, in ISO 8601, if that is not now: for
        something a plugin remembered before the workflow took its place. A
        time without a timezone is taken as UTC.
        """
        moment = datetime.datetime.now(datetime.timezone.utc)
        if at is not None:
            with contextlib.suppress(ValueError):
                moment = datetime.datetime.fromisoformat(at)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
        kept = {"value": value, "at": moment.isoformat(timespec="seconds")}
        await redis_api.hset(self._key, name, json.dumps(kept))

    async def forget(self, name: str) -> bool:
        """Drop what is kept under a name. Return whether there was anything."""
        return bool(await redis_api.hdel(self._key, name))

    async def names(self) -> list[str]:
        """Return the names that something is kept under, in order."""
        return sorted(_text(name) for name in await redis_api.hgetall(self._key))

    async def shown(self) -> list[str]:
        """Return the names to show the user: all but the engine's own note."""
        return [name for name in await self.names() if name != MOVED_MARKER]

    async def clear(self) -> None:
        """Drop everything."""
        await redis_api.delete(self._key)


class WorkflowQueue:
    """The keys of what one workflow still has to run for, oldest first.

    See ``spanreed.workflows.blocks.EventQueue``.
    """

    def __init__(self, workflow_id: str) -> None:
        """Create the queue of the workflow with this id."""
        self._id = workflow_id
        self._order_key = f"{_workflow_key(workflow_id)}:queue"
        self._tokens_key = f"{_workflow_key(workflow_id)}:queued"

    @property
    def _lock(self) -> asyncio.Lock:
        return _queue_locks.setdefault(self._id, asyncio.Lock())

    @property
    def _wakeup(self) -> asyncio.Event:
        return _queue_wakeups.setdefault(self._id, asyncio.Event())

    async def put(self, key: str) -> None:
        """Add a key, or note that a key that waits changed again."""
        async with self._lock:
            waiting = await redis_api.hget(self._tokens_key, key)
            await redis_api.hset(self._tokens_key, key, secrets.token_hex(4))
            if waiting is None:
                await redis_api.rpush(self._order_key, key)
        self._wakeup.set()

    async def keys(self) -> list[str]:
        """Return the keys that wait, oldest first."""
        return [_text(key) for key in await redis_api.lrange(self._order_key, 0, -1)]

    async def next(self) -> tuple[str, str]:
        """Wait for the oldest key. Return it with a token for ``done``."""
        while True:
            self._wakeup.clear()
            async with self._lock:
                first = await redis_api.lrange(self._order_key, 0, 0)
                if first:
                    key = _text(first[0])
                    token = await redis_api.hget(self._tokens_key, key)
                    return key, "" if token is None else _text(token)
            await self._wakeup.wait()

    async def done(self, key: str, token: str) -> None:
        """Take a key out, unless it was put again since ``next`` gave it.

        A key that was put again stays, and goes behind the others.
        """
        async with self._lock:
            current = await redis_api.hget(self._tokens_key, key)
            await redis_api.lrem(self._order_key, 1, key)
            if current is not None and _text(current) != token:
                await redis_api.rpush(self._order_key, key)
            else:
                await redis_api.hdel(self._tokens_key, key)

    async def later(self, key: str) -> None:
        """Move a key behind the others, to run for it again later."""
        async with self._lock:
            if await redis_api.lrem(self._order_key, 1, key):
                await redis_api.rpush(self._order_key, key)


class WorkflowStore:
    """The workflows of every user, and the traces of their runs."""

    async def save(self, workflow: Workflow) -> None:
        """Store a workflow, replacing the stored one with the same id."""
        await redis_api.set(_workflow_key(workflow.id), json.dumps(workflow.to_data()))
        await redis_api.sadd(_user_key(workflow.user_id), workflow.id)

    async def get(self, workflow_id: str) -> Workflow | None:
        """Return a workflow, or ``None`` if there is none with this id."""
        raw = await redis_api.get(_workflow_key(workflow_id))
        if raw is None:
            return None
        return Workflow.from_data(json.loads(_text(raw)))

    async def list_for_user(self, user_id: int) -> list[Workflow]:
        """Return the workflows of a user, ordered by name."""
        workflows = []
        for workflow_id in await redis_api.smembers(_user_key(user_id)):
            workflow = await self.get(_text(workflow_id))
            if workflow is not None:
                workflows.append(workflow)
        return sorted(workflows, key=lambda w: (w.name.lower(), w.id))

    async def delete(self, workflow: Workflow) -> None:
        """Delete a workflow with all that is kept for it."""
        key = _workflow_key(workflow.id)
        await redis_api.delete(
            key,
            f"{key}:state",
            f"{key}:memory",
            f"{key}:runs",
            f"{key}:queue",
            f"{key}:queued",
            f"{key}:last_event",
        )
        await redis_api.srem(_user_key(workflow.user_id), workflow.id)

    def trigger_state(self, workflow_id: str) -> StoredTriggerState:
        """Return what the trigger of a workflow remembers."""
        return StoredTriggerState(workflow_id)

    def memory(self, workflow_id: str) -> StoredMemory:
        """Return what the steps of a workflow remember."""
        return StoredMemory(workflow_id)

    def queue(self, workflow_id: str) -> WorkflowQueue:
        """Return the keys that a workflow still has to run for."""
        return WorkflowQueue(workflow_id)

    async def add_run(self, workflow_id: str, trace: dict[str, Any]) -> None:
        """Keep the trace of a run that is over."""
        key = f"{_workflow_key(workflow_id)}:runs"
        await redis_api.lpush(key, json.dumps(trace))
        await redis_api.ltrim(key, 0, RUNS_KEPT - 1)
        await redis_api.expire(key, int(RUNS_TTL.total_seconds()))

    async def keep_last_event(self, workflow_id: str, at: str, event: Any) -> None:
        """Keep the event that a workflow last ran for, whole.

        ``event`` is JSON data. It is dropped with the traces, ``RUNS_TTL``
        after the last run.
        """
        key = f"{_workflow_key(workflow_id)}:last_event"
        await redis_api.set(key, json.dumps({"at": at, "event": event}))
        await redis_api.expire(key, int(RUNS_TTL.total_seconds()))

    async def last_event(self, workflow_id: str) -> dict[str, Any] | None:
        """Return the event a workflow last ran for, and when: ``at``, ``event``."""
        raw = await redis_api.get(f"{_workflow_key(workflow_id)}:last_event")
        if raw is None:
            return None
        kept: dict[str, Any] = json.loads(_text(raw))
        return kept

    async def recent_runs(self, workflow_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the traces of the last runs of a workflow, newest first."""
        key = f"{_workflow_key(workflow_id)}:runs"
        return [
            json.loads(_text(raw)) for raw in await redis_api.lrange(key, 0, limit - 1)
        ]


async def emit(user_id: int, block_id: str, keys: Iterable[str]) -> None:
    """Queue keys for each workflow of a user that has this trigger.

    Only a workflow that is on gets them: one that is off does not collect
    what it missed.
    """
    keys = list(keys)
    if not keys:
        return
    store = WorkflowStore()
    for workflow in await store.list_for_user(user_id):
        if workflow.enabled and workflow.trigger.block == block_id:
            queue = store.queue(workflow.id)
            for key in keys:
                await queue.put(key)
