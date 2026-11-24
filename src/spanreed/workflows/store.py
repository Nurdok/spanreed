"""Keep workflows, their triggers' state and their runs' traces in Redis.

Keys:

- ``workflows:user_id={id}``            set of the user's workflow ids
- ``workflows:workflow:{id}``           the workflow, as JSON
- ``workflows:workflow:{id}:state``     hash of what its trigger remembers
- ``workflows:workflow:{id}:runs``      list of run traces, newest first

A workflow keeps the traces of its last ``RUNS_KEPT`` runs. They are dropped
``RUNS_TTL`` after the last run.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from spanreed.storage import redis_api
from spanreed.workflows.model import Workflow

RUNS_KEPT = 100
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
        """Delete a workflow, its trigger's state and its runs' traces."""
        key = _workflow_key(workflow.id)
        await redis_api.delete(key, f"{key}:state", f"{key}:runs")
        await redis_api.srem(_user_key(workflow.user_id), workflow.id)

    def trigger_state(self, workflow_id: str) -> StoredTriggerState:
        """Return what the trigger of a workflow remembers."""
        return StoredTriggerState(workflow_id)

    async def add_run(self, workflow_id: str, trace: dict[str, Any]) -> None:
        """Keep the trace of a run that is over."""
        key = f"{_workflow_key(workflow_id)}:runs"
        await redis_api.lpush(key, json.dumps(trace))
        await redis_api.ltrim(key, 0, RUNS_KEPT - 1)
        await redis_api.expire(key, int(RUNS_TTL.total_seconds()))

    async def recent_runs(self, workflow_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the traces of the last runs of a workflow, newest first."""
        key = f"{_workflow_key(workflow_id)}:runs"
        return [
            json.loads(_text(raw)) for raw in await redis_api.lrange(key, 0, limit - 1)
        ]
