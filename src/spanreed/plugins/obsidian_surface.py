"""Server-side driver of the Obsidian interaction surface (design §3.3-3.4).

Runs the instance->server events consumer (answers, palette command
invocations, reconnect syncs, toggle changes), the per-user command-list
publisher, and the housekeeping sweep (origin-death cancellation, instance
GC). Inert while no Obsidian instance has ever registered.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import time
from typing import Any

from spanreed.interaction import commands, engine
from spanreed.interaction import obsidian_protocol as protocol
from spanreed.interaction.api import UserInteractionApi
from spanreed.interaction.engine import UserInteractionPriority
from spanreed.interaction.obsidian_surface import ObsidianSurface
from spanreed.interaction.surface import SurfaceScope
from spanreed.plugin import Plugin
from spanreed.storage import redis_api
from spanreed.user import User

_logger = logging.getLogger(__name__)

EVENTS_POLL_TIMEOUT_S = 5
HOUSEKEEPING_INTERVAL_S = 30


def build_command_list(user: User) -> dict:
    """The per-user palette read-model: content-hash versioned (design §3.4)."""
    entries = [
        {"id": c.id, "plugin": c.plugin_canonical_name, "text": c.text}
        for c in commands.commands_for_user(user)
    ]
    canonical = json.dumps(entries, sort_keys=True)
    version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {"version": version, "commands": entries}


async def publish_command_list(user: User) -> str:
    command_list = build_command_list(user)
    await redis_api.set(protocol.command_list_key(user.id), json.dumps(command_list))
    # Nudge live instances to re-fetch; a matching version is a no-op for
    # them (no palette re-registration).
    version: str = command_list["version"]
    await protocol.fan_out(user.id, "commands-updated", {"version": version})
    return version


class ObsidianSurfacePlugin(Plugin):
    """Not a user-facing plugin: infrastructure driving the surface."""

    @classmethod
    def name(cls) -> str:
        return "Obsidian Surface"

    async def run(self) -> None:
        UserInteractionApi._ensure_default_surfaces()
        self._surface = self._get_surface()
        commands.add_change_listener(self._schedule_publish_all)
        self._last_housekeeping = 0.0
        self._logger.info("Obsidian surface driver starting")
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Obsidian surface loop error; retrying in 5s")
                await asyncio.sleep(5)

    def _get_surface(self) -> ObsidianSurface:
        for surface in UserInteractionApi._surfaces:
            if isinstance(surface, ObsidianSurface):
                return surface
        raise RuntimeError("ObsidianSurface not registered")

    async def _run_once(self) -> None:
        user_ids = await protocol.get_surface_user_ids()
        if not user_ids:
            await asyncio.sleep(EVENTS_POLL_TIMEOUT_S)
            return

        keys = [protocol.events_key(user_id) for user_id in user_ids]
        result = await redis_api.blpop(keys, timeout=EVENTS_POLL_TIMEOUT_S)
        if result is not None:
            key, raw = result
            key = key.decode() if isinstance(key, bytes) else key
            user_id = int(key.split(":")[1])
            await self._handle_raw_event(user_id, raw)

        now = time.monotonic()
        if now - self._last_housekeeping > HOUSEKEEPING_INTERVAL_S:
            self._last_housekeeping = now
            await self._housekeeping(user_ids)

    async def _handle_raw_event(self, user_id: int, raw: Any) -> None:
        try:
            event = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            kind = event["kind"]
            instance_id = event["instance_id"]
            payload = event.get("payload") or {}
        except (ValueError, KeyError, TypeError, AttributeError):
            # Malformed events are logged and skipped, never crash the
            # consumer (R8).
            self._logger.exception(f"Malformed Obsidian event: {raw!r}")
            return
        try:
            user = await User.find_by_id(user_id)
            await self.handle_event(user, instance_id, kind, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                f"Failed handling Obsidian event {kind!r} for user {user_id}"
            )

    async def handle_event(
        self, user: User, instance_id: str, kind: str, payload: dict
    ) -> None:
        self._logger.info(
            f"Obsidian event {kind!r} from instance {instance_id}" f" of user {user.id}"
        )
        if kind == "answer":
            await self._handle_answer(user, instance_id, payload)
        elif kind == "invoke-command":
            await self._handle_invoke_command(user, instance_id, payload)
        elif kind == "sync":
            await self._handle_sync(user, instance_id)
        elif kind == "instance-update":
            await self._handle_instance_update(user, instance_id, payload)
        else:
            self._logger.warning(f"Unknown Obsidian event kind {kind!r}")

    async def _handle_answer(self, user: User, instance_id: str, payload: dict) -> None:
        interaction_id = payload["interaction_id"]
        value = payload["value"]
        if not isinstance(value, (int, str)):
            raise ValueError(f"Bad answer value: {value!r}")
        resolution = engine.resolve_prompt(interaction_id, value)
        if resolution == engine.PromptResolution.ALREADY_ANSWERED:
            await self._surface.send_feedback(
                user.id,
                instance_id,
                "already-answered",
                {"interaction_id": interaction_id},
            )
        elif resolution == engine.PromptResolution.UNKNOWN:
            # Typically a pre-restart prompt: engine state is in-memory (R8).
            await self._surface.send_feedback(
                user.id,
                instance_id,
                "interaction-expired",
                {"interaction_id": interaction_id},
            )

    async def _handle_invoke_command(
        self, user: User, instance_id: str, payload: dict
    ) -> None:
        command_id = payload["command_id"]
        command = commands.get_command(command_id)
        # Server-side revalidation (R3): the palette cache may be stale.
        if command is None or command.plugin_canonical_name not in user.plugins:
            await self._surface.send_feedback(
                user.id,
                instance_id,
                "flow-error",
                {
                    "command_id": command_id,
                    "message": (
                        "This command is no longer available — the palette"
                        " may be out of date."
                    ),
                },
            )
            return

        async def command_flow() -> None:
            from spanreed.plugins.spanreed_monitor import (
                suppress_and_log_exception,
            )

            async with suppress_and_log_exception(BaseException):
                ui = await UserInteractionApi.for_user(user)
                async with ui.user_interaction(
                    priority=UserInteractionPriority.HIGH,
                    # User-initiated: the flow sticks to this instance.
                    scope=SurfaceScope(
                        origin_surface="obsidian",
                        origin_instance=instance_id,
                    ),
                ):
                    await command.callback(user)

        # Shared slot with /do: newest user-initiated flow wins (design §2.6).
        engine.start_user_flow(user.id, command_flow(), origin_instance=instance_id)

    async def _handle_sync(self, user: User, instance_id: str) -> None:
        """Reconnect sync (design §3.2): authoritative state + seq watermark."""
        pending_payload: dict | None = None
        pending = engine.get_pending_prompt_for_user(user.id)
        if pending is not None and self._prompt_targets_instance(user, instance_id):
            obsidian_state = pending.surface_state.get(ObsidianSurface.name, {})
            pending_payload = obsidian_state.get("payload")

        command_list_version: str | None = None
        raw = await redis_api.get(protocol.command_list_key(user.id))
        if raw is not None:
            try:
                command_list_version = json.loads(
                    raw.decode() if isinstance(raw, bytes) else raw
                )["version"]
            except (ValueError, KeyError, TypeError):
                pass

        await self._surface.send_feedback(
            user.id,
            instance_id,
            "sync-reply",
            {
                "pending_prompt": pending_payload,
                "command_list_version": command_list_version,
                # The client applies this reply, then processes only
                # envelopes with seq greater than this watermark.
                "seq_high_water": await protocol.get_seq_high_water(
                    user.id, instance_id
                ),
            },
        )

    def _prompt_targets_instance(self, user: User, instance_id: str) -> bool:
        scope = engine.get_current_scope(user.id)
        if not isinstance(scope, SurfaceScope):
            return True  # broadcast
        if not scope.includes(ObsidianSurface.name):
            return False
        return scope.origin_instance is None or scope.origin_instance == instance_id

    async def _handle_instance_update(
        self, user: User, instance_id: str, payload: dict
    ) -> None:
        # The client writes its registration hash directly; this event just
        # makes the server react immediately (R6).
        if payload.get("enabled") is False:
            cancelled = engine.cancel_user_flow_for_instance(instance_id)
            if cancelled:
                self._logger.info(
                    f"Toggle-off of instance {instance_id} cancelled flows"
                    f" for users {cancelled}"
                )

    async def _housekeeping(self, user_ids: list[int]) -> None:
        # Origin-death cancellation: a flow whose origin instance's presence
        # expired must not keep the interaction slot (design §2.5).
        for user_id, origin in engine.get_active_flow_origins().items():
            if not await protocol.is_instance_live(user_id, origin):
                engine.cancel_user_flow_for_instance(origin)
        for user_id in user_ids:
            await protocol.gc_dead_instances(user_id)

    # -- command-list publishing ------------------------------------------

    def _schedule_publish_all(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.create_task(self._publish_all_command_lists())

        def _log_failure(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                self._logger.error(f"Command-list publish failed: {t.exception()!r}")

        task.add_done_callback(_log_failure)

    async def _publish_all_command_lists(self) -> None:
        for user_id in await protocol.get_surface_user_ids():
            try:
                user = await User.find_by_id(user_id)
                version = await publish_command_list(user)
                self._logger.info(
                    f"Published command list v{version} for user {user_id}"
                )
            except Exception:
                self._logger.exception(
                    f"Failed publishing command list for user {user_id}"
                )
