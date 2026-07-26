"""Obsidian implementation of the user-interaction surface (design §2.2, §3).

Delivery is fan-out over per-instance Redis FIFO queues; answers come back
through the events consumer (see ``spanreed.plugins.obsidian_surface``).
Inert until an instance registers: ``is_reachable`` is False for users with
no live, enabled instance, so the facade never delivers here.
"""

import logging
import time
import uuid
from typing import Any

from spanreed.interaction import engine, obsidian_protocol as protocol
from spanreed.interaction.engine import PendingPrompt
from spanreed.interaction.surface import (
    Content,
    PromptSpec,
    SurfaceScope,
    UserInteractionSurface,
)
from spanreed.user import User

_logger = logging.getLogger(__name__)


def _content_payload(content: Content) -> dict:
    return {
        "text": content.text,
        "parse_html": content.parse_html,
        "parse_markdown": content.parse_markdown,
    }


class ObsidianSurface(UserInteractionSurface):
    name = "obsidian"

    async def is_reachable(self, user: User) -> bool:
        return bool(await protocol.get_live_instances(user.id))

    async def _target_instances(self, user: User) -> list[str]:
        """Live instances in scope: the flow's origin instance when the
        ambient scope is origin-sticky to a specific Obsidian instance,
        otherwise every live instance (broadcast/fan-out)."""
        scope = engine.get_current_scope(user.id)
        if (
            isinstance(scope, SurfaceScope)
            and scope.origin_surface == self.name
            and scope.origin_instance is not None
        ):
            if await protocol.is_instance_live(user.id, scope.origin_instance):
                return [scope.origin_instance]
            return []
        return await protocol.get_live_instances(user.id)

    # -- messages ---------------------------------------------------------

    async def send_message(self, user: User, content: Content) -> str | None:
        instances = await self._target_instances(user)
        if not instances:
            return None
        message_id = uuid.uuid4().hex
        await protocol.fan_out(
            user.id,
            "message",
            {"message_id": message_id, "content": _content_payload(content)},
            instances=instances,
        )
        return message_id

    async def edit_message(self, user: User, message: str, content: Content) -> None:
        await protocol.fan_out(
            user.id,
            "update-message",
            {"message_id": message, "content": _content_payload(content)},
        )

    async def delete_message(self, user: User, message: str) -> bool:
        delivered = await protocol.fan_out(
            user.id, "retract-message", {"message_id": message}
        )
        return bool(delivered)

    async def notify(self, user: User, content: Content) -> None:
        # Durable: the inbox entry is written regardless of reachability;
        # live instances also get a toast nudge (design §3.4, R4).
        entry = await protocol.write_inbox_entry(
            user.id,
            content.text,
            content.parse_html,
            content.parse_markdown,
        )
        await protocol.fan_out(user.id, "notification", {"entry": entry})

    async def send_multiple_messages(
        self, user: User, contents: list[Content], delay: int
    ) -> None:
        # No typing-indicator equivalent; the transcript renders in order,
        # so the inter-message delay is meaningless here.
        for content in contents:
            await self.send_message(user, content)

    # -- prompts ----------------------------------------------------------

    def _state(self, prompt: PendingPrompt) -> dict:
        state: dict = prompt.surface_state.setdefault(self.name, {})
        return state

    async def render_prompt(
        self, user: User, prompt: PendingPrompt, spec: PromptSpec
    ) -> None:
        instances = await self._target_instances(user)
        if not instances:
            # Live-prompts-only (R5): nothing queued for closed instances.
            return
        payload = {
            "interaction_id": spec.interaction_id,
            "generation": spec.generation,
            "kind": str(spec.kind),
            "prompt": _content_payload(spec.prompt),
            "choices": spec.choices,
            "columns": spec.columns,
            # Client-side staleness guard (design §3.2): a prompt popped
            # long after issuance was queued while the instance was closed
            # and must be discarded, not rendered.
            "issued_at": time.time(),
        }
        kind = "prompt" if spec.generation == 0 else "re-prompt"
        delivered = await protocol.fan_out(user.id, kind, payload, instances=instances)
        state = self._state(prompt)
        state["instances"] = delivered
        # Kept for reconnect sync-replies (the engine's PendingPrompt does
        # not retain the rendered spec).
        state["payload"] = payload

    async def _dismiss(self, user: User, prompt: PendingPrompt, reason: str) -> None:
        state = self._state(prompt)
        instances = state.get("instances")
        if not instances:
            # This surface never rendered the prompt; nothing to withdraw.
            return
        await protocol.fan_out(
            user.id,
            "dismiss-prompt",
            {"interaction_id": prompt.interaction_id, "reason": reason},
            instances=instances,
        )

    async def dismiss_prompt(self, user: User, prompt: PendingPrompt) -> None:
        await self._dismiss(user, prompt, "cancelled")

    async def cleanup_prompt(self, user: User, prompt: PendingPrompt) -> None:
        await self._dismiss(user, prompt, "answered")

    # -- feedback to a single instance ------------------------------------

    async def send_feedback(
        self, user_id: int, instance_id: str, kind: str, payload: dict
    ) -> None:
        """Route a per-instance feedback envelope (already-answered,
        interaction-expired, flow-error, sync-reply, commands-updated)."""
        await protocol.push_envelope(user_id, instance_id, kind, payload)
