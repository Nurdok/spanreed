"""The per-user interaction facade plugins use.

``UserInteractionApi`` replaces direct plugin use of ``TelegramBotApi``:
same public contract, but delivery goes through registered surfaces and
serialization through the surface-neutral engine. In phase 1 the only
surface is Telegram, so behavior is unchanged.
"""

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from spanreed.interaction import engine
from spanreed.interaction.engine import (
    PendingPrompt,
    PromptKind,
    UserInteractionPriority,
)
from spanreed.interaction.errors import PromptDeliveryError
from spanreed.interaction.surface import (
    Content,
    MessageHandle,
    PromptSpec,
    UserInteractionSurface,
)
from spanreed.user import User

if TYPE_CHECKING:
    from spanreed.plugin import Plugin

_logger = logging.getLogger(__name__)

_PROMPT_REPROMPT_INTERVAL = datetime.timedelta(minutes=60)


class UserInteractionApi:
    _surfaces: list[UserInteractionSurface] = []

    def __init__(self, user: User):
        self._user = user

    @classmethod
    def register_surface(cls, surface: UserInteractionSurface) -> None:
        if any(s.name == surface.name for s in cls._surfaces):
            raise RuntimeError(f"Surface {surface.name} already registered")
        cls._surfaces.append(surface)

    @classmethod
    def reset_surfaces_for_tests(cls) -> None:
        cls._surfaces = []

    @classmethod
    async def for_user(cls, user: User) -> "UserInteractionApi":
        # Phase 1: preserve the existing startup ordering — plugins used to
        # block on `TelegramBotApi.for_user` until the Telegram application
        # was initialized; keep that gate so behavior is unchanged.
        from spanreed.apis.telegram_bot import TelegramBotApi

        await TelegramBotApi.get_application()
        cls._ensure_default_surfaces()
        return UserInteractionApi(user)

    @classmethod
    def _ensure_default_surfaces(cls) -> None:
        if not cls._surfaces:
            from spanreed.interaction.telegram_surface import TelegramSurface

            cls._surfaces.append(TelegramSurface())

    async def _reachable_surfaces(self) -> list[UserInteractionSurface]:
        return [
            surface
            for surface in self._surfaces
            if await surface.is_reachable(self._user)
        ]

    # -- commands ---------------------------------------------------------

    @classmethod
    async def register_command(cls, plugin: "Plugin", command: Any) -> None:
        # Phase 1 shim: the command registry still lives in the Telegram
        # application's bot_data (and retains its await-on-init), because
        # /do reads it there. Phase 2 replaces this with the surface-neutral
        # CommandRegistry and rewrites /do in the same deployable unit.
        from spanreed.apis.telegram_bot import TelegramBotApi

        await TelegramBotApi.register_command(plugin, command)

    # -- sending ----------------------------------------------------------

    async def send_message(
        self,
        text: str,
        *,
        parse_html: bool = True,
        parse_markdown: bool = False,
    ) -> MessageHandle:
        content = Content(text, parse_html, parse_markdown)
        handle = MessageHandle()
        errors: list[Exception] = []
        for surface in await self._reachable_surfaces():
            try:
                message = await surface.send_message(self._user, content)
            except Exception as e:
                errors.append(e)
                continue
            if message is not None:
                handle.per_surface[surface.name] = message
        return self._delivered_or_raise(handle, errors)

    def _delivered_or_raise(
        self, handle: MessageHandle, errors: list[Exception]
    ) -> MessageHandle:
        """Broadcast semantics: succeed if >=1 surface delivered.

        With a single surface (phase 1), its error propagates unchanged —
        preserving today's fast-fail during flood bans.
        """
        if handle.per_surface:
            for error in errors:
                _logger.warning(f"Partial delivery failure: {error!r}")
            return handle
        if errors:
            raise errors[0]
        return handle

    async def notify(
        self,
        text: str,
        *,
        parse_html: bool = True,
        parse_markdown: bool = False,
    ) -> None:
        content = Content(text, parse_html, parse_markdown)
        for surface in self._surfaces:  # durable: ignores reachability
            await surface.notify(self._user, content)

    async def send_multiple_messages(
        self, *text: str, delay: int = 1, parse_html: bool = True
    ) -> None:
        contents = [Content(t, parse_html=parse_html) for t in text]
        for surface in await self._reachable_surfaces():
            await surface.send_multiple_messages(self._user, contents, delay)

    async def send_document(self, file_name: str, data: bytes) -> MessageHandle:
        handle = MessageHandle()
        errors: list[Exception] = []
        for surface in await self._reachable_surfaces():
            try:
                message = await surface.send_document(self._user, file_name, data)
            except Exception as e:
                errors.append(e)
                continue
            if message is not None:
                handle.per_surface[surface.name] = message
        return self._delivered_or_raise(handle, errors)

    async def edit_message_text(self, handle: MessageHandle, text: str) -> None:
        for surface in self._surfaces:
            message = handle.per_surface.get(surface.name)
            if message is not None:
                await surface.edit_message(self._user, message, Content(text))

    async def delete_message(self, handle: MessageHandle) -> bool:
        deleted = False
        for surface in self._surfaces:
            message = handle.per_surface.get(surface.name)
            if message is not None:
                deleted = await surface.delete_message(self._user, message) or deleted
        return deleted

    # -- prompts ----------------------------------------------------------

    async def request_user_choice(
        self,
        prompt: str,
        choices: list[str] | list[str | list[str]],
        *,
        columns: int = 1,
    ) -> int:
        result = await self._prompt(
            PromptKind.CHOICE, prompt, choices=choices, columns=columns
        )
        if not isinstance(result, int):
            raise ValueError("Expected integer from user")
        return result

    async def request_user_input(self, prompt: str) -> str:
        result = await self._prompt(PromptKind.INPUT, prompt)
        if not isinstance(result, str):
            raise ValueError("Expected string from user")
        return result

    async def _prompt(
        self,
        kind: PromptKind,
        prompt_text: str,
        choices: list[str] | list[str | list[str]] | None = None,
        columns: int = 1,
    ) -> int | str:
        pending: PendingPrompt = engine.create_prompt(self._user.id, kind)
        _logger.info(f"Waiting for prompt {pending.interaction_id} to be done")
        try:
            while True:
                if not pending.future.done():
                    await self._render_prompt(pending, prompt_text, choices, columns)
                try:
                    async with asyncio.timeout(
                        _PROMPT_REPROMPT_INTERVAL.total_seconds()
                    ):
                        result: int | str = await pending.future
                        break
                except TimeoutError:
                    # Hourly re-prompt cycle (never expires the prompt).
                    pending.generation += 1
        except asyncio.CancelledError:
            _logger.info(f"Prompt {pending.interaction_id} was cancelled")
            for surface in self._surfaces:
                await surface.dismiss_prompt(self._user, pending)
            engine.finish_prompt(pending)
            raise
        for surface in self._surfaces:
            with contextlib.suppress(Exception):
                await surface.cleanup_prompt(self._user, pending)
        engine.finish_prompt(pending)
        _logger.info(f"Prompt {pending.interaction_id} done")
        return result

    async def _render_prompt(
        self,
        pending: PendingPrompt,
        prompt_text: str,
        choices: list[str] | list[str | list[str]] | None,
        columns: int,
    ) -> None:
        spec = PromptSpec(
            interaction_id=pending.interaction_id,
            generation=pending.generation,
            kind=pending.kind,
            prompt=Content(prompt_text),
            choices=choices,
            columns=columns,
        )
        delivered = False
        errors: list[Exception] = []
        for surface in await self._reachable_surfaces():
            try:
                await surface.render_prompt(self._user, pending, spec)
                delivered = True
            except Exception as e:
                errors.append(e)
        if not delivered:
            if errors:
                raise errors[0]
            raise PromptDeliveryError(f"No reachable surface for user {self._user.id}")

    # -- serialization ----------------------------------------------------

    def user_interaction(
        self,
        *,
        propagate_preemption: bool = True,
        priority: UserInteractionPriority = UserInteractionPriority.NORMAL,
    ) -> contextlib.AbstractAsyncContextManager:
        return engine.user_interaction(
            self._user.id,
            propagate_preemption=propagate_preemption,
            priority=priority,
        )

    async def has_active_user_interaction(self) -> bool:
        return engine.has_active_user_interaction(self._user.id)
