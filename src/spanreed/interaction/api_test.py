"""Delivery rules of the interaction facade across several surfaces."""

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.interaction import engine
from spanreed.interaction.api import UserInteractionApi
from spanreed.interaction.engine import PendingPrompt
from spanreed.interaction.errors import PromptDeliveryError, SurfaceBusyError
from spanreed.interaction.surface import (
    Content,
    PromptSpec,
    SurfaceScope,
    UserInteractionSurface,
)
from spanreed.test_utils import mock_user_find_by_id

USER_ID = 9


class FakeSurface(UserInteractionSurface):
    """A surface that records what it was asked to deliver."""

    def __init__(
        self,
        name: str,
        *,
        fallback: bool = False,
        reachable: bool = True,
        unattended: bool = False,
        banned: bool = False,
    ) -> None:
        self.name = name
        self.is_fallback = fallback
        self.reachable = reachable
        self.unattended = unattended
        # A banned surface refuses every call, as Telegram does in a ban.
        self.banned = banned
        self.calls: list[str] = []

    def _do(self, call: str) -> None:
        if self.banned:
            raise SurfaceBusyError(3600)
        self.calls.append(call)

    async def is_reachable(self, user: Any) -> bool:
        return self.reachable

    async def reaches_unattended(self, user: Any) -> bool:
        return self.unattended

    async def send_message(self, user: Any, content: Content) -> Any:
        self._do(f"message:{content.text}")
        return f"{self.name}-message"

    async def notify(self, user: Any, content: Content) -> None:
        self._do(f"notify:{content.text}")

    async def send_multiple_messages(
        self, user: Any, contents: list[Content], delay: int
    ) -> None:
        self._do("multiple:" + ",".join(c.text for c in contents))

    async def send_document(self, user: Any, file_name: str, data: bytes) -> Any:
        self._do(f"document:{file_name}")
        return f"{self.name}-document"

    async def edit_message(self, user: Any, message: Any, content: Content) -> None:
        self._do(f"edit:{content.text}")

    async def delete_message(self, user: Any, message: Any) -> bool:
        self._do("delete")
        return True

    async def render_prompt(
        self, user: Any, prompt: PendingPrompt, spec: PromptSpec
    ) -> None:
        self._do(f"prompt:{spec.prompt.text}")

    async def dismiss_prompt(self, user: Any, prompt: PendingPrompt) -> None:
        self._do("dismiss")

    async def cleanup_prompt(self, user: Any, prompt: PendingPrompt) -> None:
        self._do("cleanup")


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    engine.reset_state_for_tests()
    UserInteractionApi.reset_surfaces_for_tests()
    with patch(
        "spanreed.apis.telegram_bot.TelegramBotApi.get_application", new=AsyncMock()
    ):
        yield
    UserInteractionApi.reset_surfaces_for_tests()


def _api(*surfaces: FakeSurface) -> UserInteractionApi:
    for surface in surfaces:
        UserInteractionApi.register_surface(surface)
    return asyncio.run(UserInteractionApi.for_user(mock_user_find_by_id(USER_ID)))


# --- Telegram as a fallback ---------------------------------------------------


def test_fallback_is_used_when_no_other_surface_can_alert_the_user() -> None:
    telegram = FakeSurface("telegram", fallback=True)
    # A page that is open, but with no push notifications.
    web = FakeSurface("web")
    api = _api(telegram, web)

    asyncio.run(api.send_message("hi"))
    asyncio.run(api.notify("note"))

    assert telegram.calls == ["message:hi", "notify:note"]
    assert web.calls == ["message:hi", "notify:note"]


def test_fallback_is_left_out_when_another_surface_can_alert_the_user() -> None:
    telegram = FakeSurface("telegram", fallback=True)
    web = FakeSurface("web", unattended=True)
    api = _api(telegram, web)

    async def run() -> None:
        handle = await api.send_message("hi")
        await api.notify("note")
        await api.send_multiple_messages("a", "b")
        await api.send_document("f.txt", b"x")
        await api.edit_message_text(handle, "changed")
        asker = asyncio.create_task(api.request_user_choice("Sure?", ["Yes", "No"]))
        await asyncio.sleep(0.01)
        (prompt,) = [engine.get_pending_prompt_for_user(USER_ID)]
        assert prompt is not None
        engine.resolve_prompt(prompt.interaction_id, 0)
        assert await asker == 0

    asyncio.run(run())

    # Telegram delivered nothing; it is only told that the prompt is over.
    assert telegram.calls == ["cleanup"]
    assert web.calls == [
        "message:hi",
        "notify:note",
        "multiple:a,b",
        "document:f.txt",
        "edit:changed",
        "prompt:Sure?",
        "cleanup",
    ]


def test_fallback_returns_when_the_other_surface_is_not_reachable() -> None:
    telegram = FakeSurface("telegram", fallback=True)
    web = FakeSurface("web", reachable=False, unattended=False)
    api = _api(telegram, web)

    asyncio.run(api.send_message("hi"))

    assert telegram.calls == ["message:hi"]
    assert web.calls == []


def test_a_flow_started_on_the_fallback_surface_stays_on_it() -> None:
    telegram = FakeSurface("telegram", fallback=True)
    web = FakeSurface("web", unattended=True)
    api = _api(telegram, web)

    async def run() -> None:
        async with api.user_interaction(scope=SurfaceScope(origin_surface="telegram")):
            await api.send_message("reply to /do")

    asyncio.run(run())

    assert telegram.calls == ["message:reply to /do"]
    assert web.calls == []


# --- Telegram banned ----------------------------------------------------------


def test_every_kind_of_send_reaches_the_other_surface_during_a_ban() -> None:
    """Telegram is tried (a page is open, no push) and refuses each call."""
    telegram = FakeSurface("telegram", fallback=True, banned=True)
    web = FakeSurface("web")
    api = _api(telegram, web)

    async def run() -> None:
        handle = await api.send_message("hi")
        assert list(handle.per_surface) == ["web"]
        await api.notify("note")
        await api.send_multiple_messages("a", "b")
        document = await api.send_document("f.txt", b"x")
        assert list(document.per_surface) == ["web"]
        await api.edit_message_text(handle, "changed")
        assert await api.delete_message(handle) is True
        asker = asyncio.create_task(api.request_user_input("Name?"))
        await asyncio.sleep(0.01)
        prompt = engine.get_pending_prompt_for_user(USER_ID)
        assert prompt is not None
        engine.resolve_prompt(prompt.interaction_id, "Ada")
        assert await asker == "Ada"

    asyncio.run(run())

    assert telegram.calls == []
    assert web.calls == [
        "message:hi",
        "notify:note",
        "multiple:a,b",
        "document:f.txt",
        "edit:changed",
        "delete",
        "prompt:Name?",
        "cleanup",
    ]


def test_a_message_sent_before_the_ban_can_still_be_edited_elsewhere() -> None:
    telegram = FakeSurface("telegram", fallback=True)
    web = FakeSurface("web")
    api = _api(telegram, web)

    async def run() -> None:
        handle = await api.send_message("progress 1")
        telegram.banned = True
        await api.edit_message_text(handle, "progress 2")
        assert await api.delete_message(handle) is True

    asyncio.run(run())

    assert web.calls == ["message:progress 1", "edit:progress 2", "delete"]


def test_a_cancelled_prompt_is_dismissed_elsewhere_during_a_ban() -> None:
    telegram = FakeSurface("telegram", fallback=True)
    web = FakeSurface("web")
    api = _api(telegram, web)

    async def run() -> None:
        asker = asyncio.create_task(api.request_user_input("Name?"))
        await asyncio.sleep(0.01)
        telegram.banned = True
        asker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asker
        assert engine.get_pending_prompt_for_user(USER_ID) is None

    asyncio.run(run())

    assert web.calls == ["prompt:Name?", "dismiss"]


def test_the_ban_is_reported_when_no_other_surface_can_deliver() -> None:
    """Plugins rely on this error to back off while Telegram is all there is."""
    telegram = FakeSurface("telegram", fallback=True, banned=True)
    web = FakeSurface("web", reachable=False)
    api = _api(telegram, web)

    with pytest.raises(SurfaceBusyError):
        asyncio.run(api.send_message("hi"))
    with pytest.raises(SurfaceBusyError):
        asyncio.run(api.send_multiple_messages("a", "b"))
    with pytest.raises(SurfaceBusyError):
        asyncio.run(api.request_user_input("Name?"))
    # A notification never fails: the caller is a background job.
    asyncio.run(api.notify("note"))


def test_no_reachable_surface_at_all_fails_a_prompt() -> None:
    api = _api(FakeSurface("web", reachable=False))

    with pytest.raises(PromptDeliveryError):
        asyncio.run(api.request_user_input("Name?"))
