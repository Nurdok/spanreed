"""Telegram implementation of the user-interaction surface.

A thin adapter over ``TelegramBotApi``'s delivery layer (dispatcher, pacing,
flood control — all unchanged). This is the translation boundary for errors:
``telegram.error.RetryAfter`` becomes the neutral ``SurfaceBusyError`` here,
so nothing above this module references Telegram types.
"""

import contextlib
import logging
from typing import Any, Awaitable, Callable, TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import RetryAfter

from spanreed.interaction import engine
from spanreed.interaction.engine import PendingPrompt, PromptKind
from spanreed.interaction.errors import SurfaceBusyError
from spanreed.interaction.surface import (
    Content,
    PromptSpec,
    UserInteractionSurface,
)
from spanreed.user import User

_logger = logging.getLogger(__name__)

T = TypeVar("T")


async def _translating(awaitable: Awaitable[T]) -> T:
    """Await a Telegram send, translating flood-control errors."""
    try:
        return await awaitable
    except RetryAfter as e:
        raise SurfaceBusyError(e.retry_after) from e


class TelegramSurface(UserInteractionSurface):
    name = "telegram"

    def __init__(self) -> None:
        self._bots: dict[int, Any] = {}

    async def _bot(self, user: User) -> Any:
        bot = self._bots.get(user.id)
        if bot is None:
            from spanreed.apis.telegram_bot import TelegramBotApi

            bot = await TelegramBotApi.for_user(user)
            self._bots[user.id] = bot
        return bot

    async def is_reachable(self, user: User) -> bool:
        # Phase 1: every Spanreed user is a Telegram user.
        return True

    async def send_message(self, user: User, content: Content) -> Message:
        bot = await self._bot(user)
        return await _translating(
            bot.send_message(
                content.text,
                parse_html=content.parse_html,
                parse_markdown=content.parse_markdown,
            )
        )

    async def notify(self, user: User, content: Content) -> None:
        bot = await self._bot(user)
        # notify() is durable (Redis-queued) and never raises RetryAfter.
        await bot.notify(
            content.text,
            parse_html=content.parse_html,
            parse_markdown=content.parse_markdown,
        )

    async def send_multiple_messages(
        self, user: User, contents: list[Content], delay: int
    ) -> None:
        bot = await self._bot(user)
        await _translating(
            bot.send_multiple_messages(
                *(content.text for content in contents),
                delay=delay,
                parse_html=contents[0].parse_html if contents else True,
            )
        )

    async def send_document(self, user: User, file_name: str, data: bytes) -> Message:
        bot = await self._bot(user)
        return await _translating(bot.send_document(file_name, data))

    async def edit_message(
        self, user: User, message: Message, content: Content
    ) -> None:
        bot = await self._bot(user)
        await _translating(bot.edit_message_text(message, content.text))

    async def delete_message(self, user: User, message: Message) -> bool:
        bot = await self._bot(user)
        return bool(await _translating(bot.delete_message(message)))

    # -- prompts ----------------------------------------------------------

    def _state(self, prompt: PendingPrompt) -> dict:
        state: dict = prompt.surface_state.setdefault(self.name, {})
        return state

    async def render_prompt(
        self, user: User, prompt: PendingPrompt, spec: PromptSpec
    ) -> None:
        """Send (or hourly re-send) the prompt.

        Port of the ``wait_for_user_interaction`` re-prompt cycle: on a
        re-render the previous prompt message is deleted and a surface-local
        "N pending interactions" notice is maintained.
        """
        bot = await self._bot(user)
        state = self._state(prompt)

        if spec.generation > 0:
            previous: Message | None = state.get("message")
            if previous is not None:
                if not await _translating(bot.delete_message(previous)):
                    raise RuntimeError("Failed to delete message")
            pending_count = engine.get_pending_interaction_count(user.id)
            previous_notice: Message | None = state.get("pending_notice")
            if previous_notice is not None:
                await _translating(bot.delete_message(previous_notice))
            state["pending_notice"] = await _translating(
                bot.send_message(
                    f"You have {pending_count + 1} pending interactions,"
                    " this is the first one:"
                )
            )

        if spec.kind == PromptKind.CHOICE:
            state["message"] = await self._send_choice_prompt(bot, user, prompt, spec)
        else:
            state["message"] = await _translating(bot.send_message(spec.prompt.text))

    async def _send_choice_prompt(
        self, bot: Any, user: User, prompt: PendingPrompt, spec: PromptSpec
    ) -> Message:
        from spanreed.apis.telegram_bot import CallbackData

        assert spec.choices is not None
        telegram_user_id = bot._telegram_user_id

        def make_callback_data(position: int) -> CallbackData:
            return CallbackData(prompt.interaction_id, telegram_user_id, position)

        keyboard: list[list[InlineKeyboardButton]] = [[]]
        for i, choice in enumerate(spec.choices):
            if isinstance(choice, list):
                keyboard.append([])
                for choice_str in choice:
                    keyboard[-1].append(
                        InlineKeyboardButton(
                            choice_str, callback_data=make_callback_data(i)
                        )
                    )
                keyboard.append([])
            elif isinstance(choice, str):
                button = InlineKeyboardButton(
                    choice, callback_data=make_callback_data(i)
                )
                if len(keyboard[-1]) == spec.columns:
                    keyboard.append([])
                keyboard[-1].append(button)

        return await _translating(
            bot.send_prompt_with_keyboard(
                spec.prompt.text, InlineKeyboardMarkup(keyboard)
            )
        )

    async def cleanup_prompt(self, user: User, prompt: PendingPrompt) -> None:
        # After an answer: remove the pending-interactions notice. The prompt
        # message itself is deleted by the callback handler for choices, and
        # deliberately left in place for text input (unchanged behavior).
        bot = await self._bot(user)
        notice: Message | None = self._state(prompt).get("pending_notice")
        if notice is not None:
            await _translating(bot.delete_message(notice))

    async def dismiss_prompt(self, user: User, prompt: PendingPrompt) -> None:
        # Cancellation/preemption: delete the prompt message; suppress send
        # failures (e.g. during a flood ban) so they can't mask the
        # cancellation being propagated (unchanged behavior).
        bot = await self._bot(user)
        message: Message | None = self._state(prompt).get("message")
        if message is not None:
            with contextlib.suppress(Exception):
                if not await bot.delete_message(message):
                    _logger.error("Failed to delete message")
