import asyncio
import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.interaction import engine
from spanreed.interaction.engine import PromptKind
from spanreed.interaction.surface import Content, PromptSpec, SurfaceScope
from spanreed.interaction.web_surface import (
    WebSurface,
    choice_rows,
    plain_text,
    read_inbox,
    render_html,
)
from spanreed.web import push
from spanreed.test_utils import FakeRedis, mock_user_find_by_id

USER_ID = 3


def setup_function() -> None:
    engine.reset_state_for_tests()


@pytest.fixture(autouse=True)
def push_store() -> Iterator[FakeRedis]:
    """Give push subscriptions an empty Redis: no browser is subscribed."""
    fake = FakeRedis()
    with patch("spanreed.web.push.redis_api", new=fake):
        yield fake


SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
    "keys": {"p256dh": "p", "auth": "a"},
}


async def _settle() -> None:
    """Let the pushes that a surface call started in the background finish."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_html_keeps_telegram_tags_and_escapes_the_rest() -> None:
    rendered = render_html(
        Content("<b>Hi</b> <i>there</i> <script>alert(1)</script> <pre>x</pre>")
    )
    assert rendered == (
        "<b>Hi</b> <i>there</i> &lt;script&gt;alert(1)&lt;/script&gt;" " <pre>x</pre>"
    )


def test_render_html_drops_attributes_from_allowed_tags() -> None:
    assert render_html(Content('<b onclick="x()">a</b>')) == "<b>a</b>"


def test_render_html_links_only_http() -> None:
    ok = render_html(Content('<a href="https://x.test/p?a=1&b=2">go</a>'))
    assert ok == (
        '<a href="https://x.test/p?a=1&amp;b=2" target="_blank"'
        ' rel="noopener noreferrer">go</a>'
    )
    bad = render_html(Content('<a href="javascript:alert(1)">go</a>'))
    assert "javascript" not in bad.split(">")[0]
    assert bad.startswith("<a>")


def test_render_html_plain_text_is_fully_escaped() -> None:
    assert render_html(Content("<b>x</b>", parse_html=False)) == (
        "&lt;b&gt;x&lt;/b&gt;"
    )


def test_choice_rows_matches_telegram_layout() -> None:
    assert choice_rows(["a", "b", "c"], 2) == [
        [{"label": "a", "value": 0}, {"label": "b", "value": 1}],
        [{"label": "c", "value": 2}],
    ]
    mixed: list[str | list[str]] = ["a", ["x", "y"], "b"]
    assert choice_rows(mixed, 1) == [
        [{"label": "a", "value": 0}],
        [{"label": "x", "value": 1}, {"label": "y", "value": 1}],
        [{"label": "b", "value": 2}],
    ]


# ---------------------------------------------------------------------------
# Feed and surface contract
# ---------------------------------------------------------------------------


def test_reachable_only_while_a_browser_polls() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        assert not await surface.is_reachable(user)
        surface.mark_seen(USER_ID)
        assert await surface.is_reachable(user)
        with patch("spanreed.interaction.web_surface.time.monotonic") as clock:
            clock.return_value = 10**9
            assert not await surface.is_reachable(user)

    asyncio.run(run())


def test_long_poll_wakes_on_publish() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        poll = asyncio.create_task(surface.wait_for_events(USER_ID, 0, timeout=5))
        await asyncio.sleep(0.01)
        assert not poll.done()
        await surface.send_message(user, Content("hello"))
        seq, events, reset = await asyncio.wait_for(poll, 1)
        assert (seq, reset) == (1, False)
        assert [e["html"] for e in events] == ["hello"]

        # A cursor from before a restart (ahead of the feed) asks for reset.
        _, _, reset = await surface.wait_for_events(USER_ID, 99, timeout=0)
        assert reset

    asyncio.run(run())


def test_prompt_lifecycle_tracks_open_prompt() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        pending = engine.create_prompt(USER_ID, PromptKind.CHOICE)
        spec = PromptSpec(
            interaction_id=pending.interaction_id,
            generation=0,
            kind=PromptKind.CHOICE,
            prompt=Content("Pick"),
            choices=["Yes", "No"],
        )
        await surface.render_prompt(user, pending, spec)
        open_prompt = surface.open_prompt(USER_ID)
        assert open_prompt is not None
        assert open_prompt["rows"] == [
            [{"label": "Yes", "value": 0}],
            [{"label": "No", "value": 1}],
        ]
        surface.record_answer(USER_ID, pending.interaction_id, 1, open_prompt)
        await surface.cleanup_prompt(user, pending)
        assert surface.open_prompt(USER_ID) is None
        kinds = [(e["type"], e.get("text")) for e in surface.recent_events(USER_ID)]
        assert kinds == [("prompt", None), ("answer", "No"), ("close-prompt", None)]

    asyncio.run(run())


def test_close_is_noop_for_prompts_never_rendered_here() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        pending = engine.create_prompt(USER_ID, PromptKind.INPUT)
        await surface.dismiss_prompt(user, pending)
        assert surface.recent_events(USER_ID) == []

    asyncio.run(run())


def test_notify_is_durable_in_the_inbox() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)
    fake = FakeRedis()

    async def run() -> None:
        await surface.notify(user, Content("<b>ping</b>"))
        inbox = await read_inbox(USER_ID)
        assert [entry["html"] for entry in inbox] == ["<b>ping</b>"]
        assert surface.recent_events(USER_ID)[-1]["type"] == "notification"

    with patch("spanreed.interaction.web_surface.redis_api", new=fake):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Push notifications
# ---------------------------------------------------------------------------


def test_plain_text_strips_markup_and_shortens() -> None:
    assert plain_text(Content("<b>Did you</b>\n  exercise &amp; stretch?")) == (
        "Did you exercise & stretch?"
    )
    assert plain_text(Content("<b>kept</b>", parse_html=False)) == "<b>kept</b>"
    assert len(plain_text(Content("x" * 1000))) == 240


def test_reachable_and_unattended_while_a_browser_is_subscribed() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        assert not await surface.is_reachable(user)
        assert not await surface.reaches_unattended(user)
        # A page that is open makes it reachable, but cannot alert the user.
        surface.mark_seen(USER_ID)
        assert await surface.is_reachable(user)
        assert not await surface.reaches_unattended(user)

        await push.subscribe(USER_ID, SUBSCRIPTION)
        with patch("spanreed.interaction.web_surface.time.monotonic") as clock:
            clock.return_value = 10**9  # no page has polled for a long time
            assert await surface.is_reachable(user)
            assert await surface.reaches_unattended(user)

    asyncio.run(run())


def test_prompts_messages_and_notifications_are_pushed() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)
    sent: list[dict] = []

    async def sender(_subscription: dict, data: str, _key: str) -> None:
        sent.append(json.loads(data))

    async def run() -> None:
        await push.subscribe(USER_ID, SUBSCRIPTION)
        await surface.send_message(user, Content("<b>Hello</b>"))
        await surface.notify(user, Content("Logged steps"))
        prompt = engine.create_prompt(USER_ID, PromptKind.CHOICE)
        spec = PromptSpec(
            interaction_id=prompt.interaction_id,
            generation=0,
            kind=PromptKind.CHOICE,
            prompt=Content("Did you exercise?"),
            choices=["Yes", "No"],
        )
        await surface.render_prompt(user, prompt, spec)
        # Edits, deletions and closing a prompt are not worth a notification.
        message = await surface.send_message(user, Content("temp"))
        await surface.edit_message(user, message, Content("changed"))
        await surface.delete_message(user, message)
        await surface.cleanup_prompt(user, prompt)
        await _settle()
        tag = f"prompt-{prompt.interaction_id}"
        assert [(p["kind"], p["title"], p["body"], p["tag"]) for p in sent] == [
            ("message", "Spanreed", "Hello", None),
            ("notification", "Spanreed", "Logged steps", None),
            ("prompt", "Spanreed asks", "Did you exercise?", tag),
            ("message", "Spanreed", "temp", None),
        ]

    with (
        patch("spanreed.interaction.web_surface.redis_api", new=FakeRedis()),
        patch("spanreed.web.push._sender", new=sender),
    ):
        asyncio.run(run())


def test_nothing_is_pushed_inside_a_flow_started_from_the_web_ui() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)
    sender = AsyncMock()

    async def run() -> None:
        await push.subscribe(USER_ID, SUBSCRIPTION)
        async with engine.user_interaction(
            USER_ID, scope=SurfaceScope(origin_surface="web")
        ):
            await surface.send_message(user, Content("Reply on the page"))
            await _settle()
        sender.assert_not_awaited()
        # The same message outside such a flow is pushed.
        await surface.send_message(user, Content("Unprompted"))
        await _settle()
        sender.assert_awaited_once()

    with patch("spanreed.web.push._sender", new=sender):
        asyncio.run(run())


def test_a_failing_push_does_not_fail_the_message() -> None:
    surface = WebSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        await push.subscribe(USER_ID, SUBSCRIPTION)
        message_id = await surface.send_message(user, Content("still delivered"))
        await _settle()
        assert message_id
        assert surface.recent_events(USER_ID)[-1]["type"] == "message"

    with patch("spanreed.web.push._sender", new=AsyncMock(side_effect=OSError("down"))):
        asyncio.run(run())
