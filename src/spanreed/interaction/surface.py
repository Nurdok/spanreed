"""The surface abstraction: how one delivery channel talks to a user.

A surface owns delivery/rendering/answer-collection for its channel
(Telegram today; Obsidian in a later phase). Content is exactly what plugins
pass today — text plus the Telegram-era parse flags — and each surface owns
rendering it appropriately (design doc §2.2).
"""

import abc
from dataclasses import dataclass, field
from typing import Any

from spanreed.interaction.engine import PendingPrompt, PromptKind
from spanreed.user import User


@dataclass(frozen=True)
class Content:
    text: str
    parse_html: bool = True
    parse_markdown: bool = False


@dataclass(frozen=True)
class SurfaceScope:
    """Which surfaces a flow's conversational traffic goes to (design §2.4).

    ``None`` scope = broadcast (proactive flows). A user-initiated flow sets
    ``origin_surface`` (and, for Obsidian, ``origin_instance``) so the whole
    conversation sticks to where it started. ``notify()`` always ignores
    scope.
    """

    origin_surface: str | None = None
    origin_instance: str | None = None

    def includes(self, surface_name: str) -> bool:
        if self.origin_surface is None:
            return True
        return surface_name == self.origin_surface


@dataclass(frozen=True)
class PromptSpec:
    """Everything a surface needs to render one generation of a prompt."""

    interaction_id: str
    generation: int
    kind: PromptKind
    prompt: Content
    # Advisory layout hints (nested lists = explicit button rows); surfaces
    # may render their own layout.
    choices: list[str] | list[str | list[str]] | None = None
    columns: int = 1


@dataclass
class MessageHandle:
    """One logical message, delivered as N surface-local messages.

    ``per_surface`` maps surface name -> the surface's own message object
    (e.g. a ``telegram.Message``), so edit/delete can fan out to exactly the
    surfaces that delivered.
    """

    per_surface: dict[str, Any] = field(default_factory=dict)


class UserInteractionSurface(abc.ABC):
    name: str

    @abc.abstractmethod
    async def is_reachable(self, user: User) -> bool: ...

    @abc.abstractmethod
    async def send_message(self, user: User, content: Content) -> Any:
        """Deliver a conversational message; returns the surface message id."""

    @abc.abstractmethod
    async def notify(self, user: User, content: Content) -> None:
        """Durable fire-and-forget notification."""

    @abc.abstractmethod
    async def send_multiple_messages(
        self, user: User, contents: list[Content], delay: int
    ) -> None: ...

    @abc.abstractmethod
    async def render_prompt(
        self, user: User, prompt: PendingPrompt, spec: PromptSpec
    ) -> None:
        """Render (generation 0) or re-render (generation > 0) a prompt.

        Per-prompt delivery state lives in ``prompt.surface_state[self.name]``.
        """

    @abc.abstractmethod
    async def dismiss_prompt(self, user: User, prompt: PendingPrompt) -> None:
        """Withdraw a prompt (cancelled/preempted); best-effort."""

    @abc.abstractmethod
    async def cleanup_prompt(self, user: User, prompt: PendingPrompt) -> None:
        """Tidy up after a prompt was answered (e.g. remove glue messages)."""

    # Optional-capability primitives: default implementations degrade
    # gracefully (no-op / None).

    async def send_document(self, user: User, file_name: str, data: bytes) -> Any:
        return None

    async def edit_message(self, user: User, message: Any, content: Content) -> None:
        return None

    async def delete_message(self, user: User, message: Any) -> bool:
        return False
