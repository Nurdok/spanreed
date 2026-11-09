import asyncio
from unittest.mock import patch

from spanreed.interaction import engine
from spanreed.interaction.engine import PromptKind
from spanreed.interaction.surface import Content, PromptSpec
from spanreed.interaction.web_surface import (
    WebSurface,
    choice_rows,
    read_inbox,
    render_html,
)
from spanreed.test_utils import FakeRedis, mock_user_find_by_id

USER_ID = 3


def setup_function() -> None:
    engine.reset_state_for_tests()


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
