"""Web implementation of the user-interaction surface.

The browser UI under ``/app`` (see ``spanreed.web``) is a third surface next
to Telegram and Obsidian. It exists so the user can run their commands when
the Telegram bot is flood-limited: a flow started from the web UI is scoped
to this surface (``SurfaceScope(origin_surface="web")``), so none of its
traffic touches Telegram.

Delivery is an in-process, per-user event feed that browsers long-poll.
Like the engine's pending prompts, the feed is process-local and does not
survive a restart; ``notify()`` is the exception and is also written to a
small Redis inbox so it stays durable.

The surface is reachable only while a browser for that user polled within
``PRESENCE_TTL_S``, so broadcast (proactive) traffic reaches the web UI only
when it is actually open.
"""

from __future__ import annotations

import asyncio
import collections
import html
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from spanreed.interaction.engine import PendingPrompt
from spanreed.interaction.surface import (
    Content,
    PromptSpec,
    UserInteractionSurface,
)
from spanreed.storage import redis_api
from spanreed.user import User

SURFACE_NAME = "web"

# A browser polls at least every LONG_POLL_TIMEOUT_S; a little slack on top.
PRESENCE_TTL_S = 60
FEED_MAX_EVENTS = 300
DOCUMENTS_MAX = 20
INBOX_MAX = 50


def inbox_key(user_id: int) -> str:
    return f"web-surface:{user_id}:inbox"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# The tags Telegram's HTML parse mode accepts, which is what plugins write.
_ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "tg-spoiler",
}
_TAG_RE = re.compile(r"&lt;(/?)([a-zA-Z-]+)((?:\s.*?)?)&gt;")
# Matches the (already escaped) attribute text of an <a> tag; the captured
# URL is still escaped, which is what goes back into the attribute.
_HREF_RE = re.compile(
    r"^\s+href=(?:&quot;|&#x27;)?(https?://(?:[^\s&]|&amp;)+?)(?:&quot;|&#x27;|\s|$)"
)


def render_html(content: Content) -> str:
    """Render plugin content as safe HTML for the browser.

    Everything is escaped first; then the Telegram-HTML tags are restored
    (no attributes, except an http(s) ``href`` on ``<a>``). Text that was
    not sent as HTML stays fully escaped. Newlines are kept by the page's
    ``white-space: pre-wrap``.
    """
    escaped = html.escape(content.text)
    if not content.parse_html:
        return escaped

    def restore(match: re.Match[str]) -> str:
        closing, tag, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if tag in _ALLOWED_TAGS:
            if tag == "tg-spoiler":
                return "</span>" if closing else '<span class="spoiler">'
            return f"<{closing}{tag}>"
        if tag == "a":
            if closing:
                return "</a>"
            href = _HREF_RE.match(attrs or "")
            if href:
                return (
                    f'<a href="{href.group(1)}" target="_blank"'
                    ' rel="noopener noreferrer">'
                )
            return "<a>"
        return match.group(0)

    return _TAG_RE.sub(restore, escaped)


def choice_rows(
    choices: list[str] | list[str | list[str]], columns: int
) -> list[list[dict[str, Any]]]:
    """Lay out choices as rows of ``{label, value}`` buttons.

    Mirrors the Telegram keyboard layout: plain strings fill rows of
    ``columns``; a nested list is its own row, and each of its buttons
    answers with the nested list's index.
    """
    rows: list[list[dict[str, Any]]] = [[]]
    for i, choice in enumerate(choices):
        if isinstance(choice, list):
            rows.append([{"label": label, "value": i} for label in choice])
            rows.append([])
        else:
            if len(rows[-1]) >= max(columns, 1):
                rows.append([])
            rows[-1].append({"label": choice, "value": i})
    return [row for row in rows if row]


# ---------------------------------------------------------------------------
# Per-user feed
# ---------------------------------------------------------------------------


@dataclass
class _Feed:
    seq: int = 0
    events: collections.deque[dict[str, Any]] = field(
        default_factory=lambda: collections.deque(maxlen=FEED_MAX_EVENTS)
    )
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    last_seen: float = 0.0
    # The prompt event currently shown (for page loads mid-flow).
    open_prompt: dict[str, Any] | None = None
    documents: collections.OrderedDict[str, tuple[str, bytes]] = field(
        default_factory=collections.OrderedDict
    )


class WebSurface(UserInteractionSurface):
    name = SURFACE_NAME

    def __init__(self) -> None:
        self._feeds: dict[int, _Feed] = {}

    def _feed(self, user_id: int) -> _Feed:
        feed = self._feeds.get(user_id)
        if feed is None:
            feed = self._feeds[user_id] = _Feed()
        return feed

    # -- browser side -----------------------------------------------------

    def mark_seen(self, user_id: int) -> None:
        self._feed(user_id).last_seen = time.monotonic()

    def current_seq(self, user_id: int) -> int:
        return self._feed(user_id).seq

    def recent_events(self, user_id: int) -> list[dict[str, Any]]:
        return list(self._feed(user_id).events)

    def open_prompt(self, user_id: int) -> dict[str, Any] | None:
        return self._feed(user_id).open_prompt

    def get_document(self, user_id: int, document_id: str) -> tuple[str, bytes] | None:
        return self._feed(user_id).documents.get(document_id)

    async def wait_for_events(
        self, user_id: int, after: int, timeout: float
    ) -> tuple[int, list[dict[str, Any]], bool]:
        """Long-poll: events with ``seq > after``.

        Returns ``(seq, events, reset)``. ``reset`` is True when the
        browser's cursor is ahead of the feed (server restarted) or older
        than the retained window; the browser then reloads full state.
        """
        feed = self._feed(user_id)
        feed.last_seen = time.monotonic()
        if after > feed.seq or (feed.events and after < feed.events[0]["seq"] - 1):
            return feed.seq, [], True
        if after == feed.seq:
            event = feed.changed
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except TimeoutError:
                pass
        feed.last_seen = time.monotonic()
        return feed.seq, [e for e in feed.events if e["seq"] > after], False

    def _publish(self, user_id: int, event: dict[str, Any]) -> dict[str, Any]:
        feed = self._feed(user_id)
        feed.seq += 1
        event = {"seq": feed.seq, "ts": time.time(), **event}
        feed.events.append(event)
        # Wake every waiting poll, then arm a fresh event for the next one.
        feed.changed.set()
        feed.changed = asyncio.Event()
        return event

    # -- surface contract -------------------------------------------------

    async def is_reachable(self, user: User) -> bool:
        feed = self._feeds.get(user.id)
        return feed is not None and time.monotonic() - feed.last_seen < PRESENCE_TTL_S

    async def send_message(self, user: User, content: Content) -> str:
        message_id = uuid.uuid4().hex
        self._publish(
            user.id,
            {"type": "message", "id": message_id, "html": render_html(content)},
        )
        return message_id

    async def edit_message(self, user: User, message: Any, content: Content) -> None:
        self._publish(
            user.id,
            {"type": "edit", "id": message, "html": render_html(content)},
        )

    async def delete_message(self, user: User, message: Any) -> bool:
        self._publish(user.id, {"type": "delete", "id": message})
        return True

    async def notify(self, user: User, content: Content) -> None:
        entry = {"ts": time.time(), "html": render_html(content)}
        await redis_api.rpush(inbox_key(user.id), json.dumps(entry))
        await redis_api.ltrim(inbox_key(user.id), -INBOX_MAX, -1)
        self._publish(user.id, {"type": "notification", **entry})

    async def send_multiple_messages(
        self, user: User, contents: list[Content], delay: int
    ) -> None:
        for content in contents:
            await self.send_message(user, content)

    async def send_document(self, user: User, file_name: str, data: bytes) -> str:
        feed = self._feed(user.id)
        document_id = uuid.uuid4().hex
        feed.documents[document_id] = (file_name, data)
        while len(feed.documents) > DOCUMENTS_MAX:
            feed.documents.popitem(last=False)
        self._publish(
            user.id,
            {"type": "document", "id": document_id, "name": file_name},
        )
        return document_id

    async def render_prompt(
        self, user: User, prompt: PendingPrompt, spec: PromptSpec
    ) -> None:
        event = self._publish(
            user.id,
            {
                "type": "prompt",
                "interaction_id": spec.interaction_id,
                "kind": str(spec.kind),
                "html": render_html(spec.prompt),
                "rows": (
                    choice_rows(spec.choices, spec.columns)
                    if spec.choices is not None
                    else None
                ),
            },
        )
        self._feed(user.id).open_prompt = event
        prompt.surface_state[self.name] = {"rendered": True}

    async def _close_prompt(
        self, user: User, prompt: PendingPrompt, reason: str
    ) -> None:
        if not prompt.surface_state.get(self.name):
            return  # never rendered here
        feed = self._feed(user.id)
        if (
            feed.open_prompt is not None
            and feed.open_prompt["interaction_id"] == prompt.interaction_id
        ):
            feed.open_prompt = None
        self._publish(
            user.id,
            {
                "type": "close-prompt",
                "interaction_id": prompt.interaction_id,
                "reason": reason,
            },
        )

    async def dismiss_prompt(self, user: User, prompt: PendingPrompt) -> None:
        await self._close_prompt(user, prompt, "cancelled")

    async def cleanup_prompt(self, user: User, prompt: PendingPrompt) -> None:
        await self._close_prompt(user, prompt, "answered")

    # -- flow status --------------------------------------------------------

    def flow_started(self, user_id: int, flow_id: str, title: str) -> None:
        self._publish(
            user_id, {"type": "flow-started", "flow_id": flow_id, "title": title}
        )

    def record_answer(
        self,
        user_id: int,
        interaction_id: str,
        value: int | str,
        prompt: dict[str, Any] | None,
    ) -> None:
        """Echo the user's answer into the transcript (all open tabs).

        ``prompt`` is the prompt event that was open when the answer came
        in, used to turn a choice index back into its button label.
        """
        text = value if isinstance(value, str) else str(value)
        if (
            isinstance(value, int)
            and prompt is not None
            and prompt["interaction_id"] == interaction_id
        ):
            for row in prompt.get("rows") or []:
                for button in row:
                    if button["value"] == value:
                        text = button["label"]
                        break
        self._publish(user_id, {"type": "answer", "text": text})

    def flow_finished(
        self, user_id: int, flow_id: str, error: str | None = None
    ) -> None:
        """Tell open pages a web-started flow ended (so they unlock)."""
        self._publish(
            user_id, {"type": "flow-finished", "flow_id": flow_id, "error": error}
        )


async def read_inbox(user_id: int) -> list[dict[str, Any]]:
    raw_entries = await redis_api.lrange(inbox_key(user_id), 0, -1)
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        try:
            entries.append(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
        except ValueError:
            continue
    return entries
