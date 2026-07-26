"""Redis protocol between the server and Obsidian instances (design §3).

Key layout (all namespaced ``obsidian-surface:{user_id}:``):

- ``…:instances``            — hash: instance_id -> registration JSON
                               (vault, device, enabled, registered_at,
                               last_seen)
- ``…:{iid}:presence``       — SET EX key refreshed by the instance's poll
                               loop; liveness signal
- ``…:{iid}:inbound``        — FIFO delivery list (server RPUSH, instance
                               BLPOP), capped
- ``…:{iid}:seq``            — per-instance monotonic envelope counter
- ``…:events``               — FIFO list of instance->server events (server
                               is the only consumer)
- ``…:command-list``         — JSON read-model {version, commands}
- ``…:inbox``                — capped notification list (read
                               non-destructively by all instances)

Plus one global set ``obsidian-surface:users`` of user ids that have (ever)
registered an instance — the events consumer polls those users' queues.

Ordering rule (design §3): every queue here is FIFO — producer RPUSH,
consumer BLPOP; caps keep the newest at the tail (``LTRIM -N -1``). The
prompt/dismiss and dismiss/new-prompt orderings depend on this.
"""

import datetime
import json
import logging
import time
from typing import Any

from spanreed.storage import redis_api

_logger = logging.getLogger(__name__)

USERS_SET_KEY = "obsidian-surface:users"

INBOUND_QUEUE_MAX = 100
INBOX_MAX = 200
# An instance whose last_seen is older than this gets garbage-collected.
INSTANCE_GC_AGE = datetime.timedelta(days=30)


def _base(user_id: int) -> str:
    return f"obsidian-surface:{user_id}"


def instances_key(user_id: int) -> str:
    return f"{_base(user_id)}:instances"


def presence_key(user_id: int, instance_id: str) -> str:
    return f"{_base(user_id)}:{instance_id}:presence"


def inbound_key(user_id: int, instance_id: str) -> str:
    return f"{_base(user_id)}:{instance_id}:inbound"


def seq_key(user_id: int, instance_id: str) -> str:
    return f"{_base(user_id)}:{instance_id}:seq"


def events_key(user_id: int) -> str:
    return f"{_base(user_id)}:events"


def command_list_key(user_id: int) -> str:
    return f"{_base(user_id)}:command-list"


def inbox_key(user_id: int) -> str:
    return f"{_base(user_id)}:inbox"


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def get_instances(user_id: int) -> dict[str, dict]:
    """All registered instances for a user: instance_id -> registration."""
    raw = await redis_api.hgetall(instances_key(user_id))
    instances: dict[str, dict] = {}
    for instance_id, registration_json in raw.items():
        try:
            instances[_decode(instance_id)] = json.loads(_decode(registration_json))
        except (ValueError, TypeError):
            _logger.exception(
                f"Malformed instance registration for user {user_id}:"
                f" {registration_json!r}"
            )
    return instances


async def is_instance_live(user_id: int, instance_id: str) -> bool:
    return bool(await redis_api.exists(presence_key(user_id, instance_id)))


async def get_live_instances(user_id: int) -> list[str]:
    """Instance ids that are enabled and have a live presence key."""
    live: list[str] = []
    for instance_id, registration in (await get_instances(user_id)).items():
        if not registration.get("enabled", False):
            continue
        if await is_instance_live(user_id, instance_id):
            live.append(instance_id)
    return live


async def push_envelope(
    user_id: int, instance_id: str, kind: str, payload: dict
) -> int:
    """Deliver one envelope to one instance's FIFO inbound queue.

    Returns the envelope's per-instance sequence number.
    """
    seq = int(await redis_api.incr(seq_key(user_id, instance_id)))
    envelope = json.dumps({"seq": seq, "kind": kind, "payload": payload})
    key = inbound_key(user_id, instance_id)
    await redis_api.rpush(key, envelope)
    # Keep the newest N (tail); a wedged instance can't grow unboundedly.
    await redis_api.ltrim(key, -INBOUND_QUEUE_MAX, -1)
    return seq


async def get_seq_high_water(user_id: int, instance_id: str) -> int:
    value = await redis_api.get(seq_key(user_id, instance_id))
    return int(value) if value is not None else 0


async def fan_out(
    user_id: int,
    kind: str,
    payload: dict,
    *,
    instances: list[str] | None = None,
) -> list[str]:
    """Deliver an envelope to the given instances (default: all live ones).

    Returns the instance ids actually delivered to.
    """
    if instances is None:
        instances = await get_live_instances(user_id)
    for instance_id in instances:
        await push_envelope(user_id, instance_id, kind, payload)
    return instances


async def write_inbox_entry(
    user_id: int, text: str, parse_html: bool, parse_markdown: bool
) -> dict:
    """Append a durable notification to the user's inbox (capped)."""
    import uuid

    entry = {
        "entry_id": uuid.uuid4().hex,
        "ts": time.time(),
        "text": text,
        "parse_html": parse_html,
        "parse_markdown": parse_markdown,
    }
    key = inbox_key(user_id)
    await redis_api.rpush(key, json.dumps(entry))
    await redis_api.ltrim(key, -INBOX_MAX, -1)
    return entry


async def get_surface_user_ids() -> list[int]:
    """User ids that have registered Obsidian instances (events consumers)."""
    user_ids: list[int] = []
    for member in await redis_api.smembers(USERS_SET_KEY):
        try:
            user_ids.append(int(_decode(member)))
        except (TypeError, ValueError):
            _logger.warning(f"Malformed user id in {USERS_SET_KEY}: {member!r}")
    return user_ids


async def gc_dead_instances(user_id: int) -> list[str]:
    """Drop instances not seen for over INSTANCE_GC_AGE; returns dropped ids.

    ``last_seen`` exists in the registration hash precisely because the
    presence key's short TTL leaves no trace to GC by.
    """
    dropped: list[str] = []
    now = time.time()
    for instance_id, registration in (await get_instances(user_id)).items():
        last_seen = registration.get("last_seen")
        if last_seen is None:
            continue
        if now - float(last_seen) > INSTANCE_GC_AGE.total_seconds():
            await redis_api.hdel(instances_key(user_id), instance_id)
            await redis_api.delete(
                inbound_key(user_id, instance_id),
                seq_key(user_id, instance_id),
                presence_key(user_id, instance_id),
            )
            dropped.append(instance_id)
            _logger.info(f"GC'd dead Obsidian instance {instance_id} of user {user_id}")
    return dropped
