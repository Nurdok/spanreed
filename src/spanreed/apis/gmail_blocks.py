"""The Gmail blocks for workflows.

The plugin holds the Gmail login. The trigger here asks Gmail, at an
interval, for the emails that came since the last time, and gives each one
that matches its filter as an event. The actions get the attachments of an
email and find the links in its text.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from spanreed.apis.gmail import EmailMessage, GmailApi
from spanreed.interaction import UserInteractionApi
from spanreed.user import User
from spanreed.workflows.blocks import (
    Action,
    Block,
    Poll,
    PollTrigger,
    Repeat,
    StepContext,
    TriggerContext,
)
from spanreed.workflows.files import File, file_of, safe_name
from spanreed.config_form import described

_logger = logging.getLogger(__name__)

# A poll asks for the emails since the one before it, and this much more:
# an email can reach Gmail a little after the time it carries.
OVERLAP = datetime.timedelta(hours=1)
# How far back the first poll of a workflow looks.
FIRST_LOOK_BACK = datetime.timedelta(hours=1)
# How many emails a run by hand over the inbox looks at.
INBOX_LIMIT = 200

_ANY_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_REGEX_HELP = "A regular expression, without regard to case. Empty: any."


def _now() -> datetime.datetime:
    # Indirection so that tests can set the time of a poll.
    return datetime.datetime.now(datetime.timezone.utc)


async def ensure_authenticated(user: User) -> bool:
    """Return whether Gmail can be read for a user. If not, send a login link.

    A login that Google no longer accepts would otherwise fail in silence
    at every poll. The link is sent once: while one is waiting to be used,
    ``start_authentication`` raises, and the OAuth redirect ends the wait.
    """
    gmail = await GmailApi.for_user(user)
    if await gmail.is_authenticated():
        return True
    if not await GmailApi.is_app_configured():
        _logger.warning("Gmail app credentials missing; cannot prompt re-auth.")
        return False
    try:
        flow = await GmailApi.start_authentication(user)
    except ValueError:
        # A link is waiting to be used already.
        return False
    await flow.get_done_event()
    auth_url = await flow.get_auth_url()
    bot = await UserInteractionApi.for_user(user)
    await bot.send_message(
        f'⚠️ Gmail access has expired. Click <a href="{auth_url}">here</a>'
        f" to re-authenticate and resume email monitoring."
    )
    return False


@dataclass
class EmailFilter(Repeat):
    """How often to ask Gmail, and which emails to run for."""

    every: int = 15
    unit: Literal["minutes", "hours", "days"] = "minutes"
    sender: str = field(default="", metadata={"config_help": _REGEX_HELP})
    subject: str = field(default="", metadata={"config_help": _REGEX_HELP})
    body: str = field(default="", metadata={"config_help": _REGEX_HELP})
    attachments: Literal["any", "yes", "no"] = "any"

    def __post_init__(self) -> None:
        """Raise ``ValueError`` if a pattern is not a regular expression."""
        super().__post_init__()
        for name in ("sender", "subject", "body"):
            try:
                re.compile(getattr(self, name))
            except re.error as e:
                raise ValueError(
                    f"{name.capitalize()}: this is not a regular expression ({e})."
                ) from None

    def matches(self, email: EmailMessage) -> bool:
        """Return whether an email passes every part of the filter."""
        for pattern, text in (
            (self.sender, email.sender),
            (self.subject, email.subject),
            (self.body, email.body),
        ):
            if pattern and not re.search(pattern, text, re.IGNORECASE):
                return False
        if self.attachments == "any":
            return True
        return email.has_attachments == (self.attachments == "yes")


@dataclass
class Email:
    """An email."""

    id: str = described(
        "The id that Gmail gives the email. “Get attachments” takes it",
    )
    sender: str = described(
        "Who sent it, as the email says: Shop <shop@example.com>",
    )
    sender_name: str = described(
        "The sender up to the @, without what a file name cannot hold",
    )
    subject: str = described("The subject of the email")
    subject_name: str = described(
        "The subject without what a file name cannot hold, cut at 50 " "characters",
    )
    date: str = described("The day the email carries, as 2026-05-09")
    received: str = described(
        "The day and the time the email carries, as 2026-05-09 18:30:00",
    )
    snippet: str = described(
        "The first words of the email, as Gmail shows them in a list",
    )
    body: str = described("The text of the email")
    has_attachments: bool = described("Whether the email has attachments")


def email_event(email: EmailMessage) -> Email:
    """Return an email as the event of the trigger."""
    sender_name = email.sender.split("@")[0] if "@" in email.sender else email.sender
    return Email(
        id=email.id,
        sender=email.sender,
        sender_name=safe_name(sender_name),
        subject=email.subject,
        subject_name=safe_name(re.sub(r"[^\w\-. ]", "_", email.subject)[:50]),
        date=email.date.strftime("%Y-%m-%d"),
        received=email.date.strftime("%Y-%m-%d %H:%M:%S"),
        snippet=email.snippet,
        body=email.body,
        has_attachments=email.has_attachments,
    )


_CONFIRM = (
    "Run for every matching email? Up to {limit} emails are looked at, and "
    "each match runs the steps for real."
).format(limit=INBOX_LIMIT)


@dataclass
class InboxChoice:
    """Which emails a manual run covers."""

    which: Literal["New emails", "All inbox emails", "Starred inbox emails"] = field(
        metadata={"prompt": "Run for which emails?"}
    )
    confirm_inbox: Literal["Yes"] = field(
        default="Yes",
        metadata={"prompt": _CONFIRM, "ask_if": {"which": "All inbox emails"}},
    )
    confirm_starred: Literal["Yes"] = field(
        default="Yes",
        metadata={"prompt": _CONFIRM, "ask_if": {"which": "Starred inbox emails"}},
    )


class NewEmail(PollTrigger[EmailFilter, Email]):
    """Fire for each email that reaches Gmail and matches the filter.

    An email fires once. By hand, the workflow can also run for the emails
    that are in the inbox, whether it ran for them before or not.
    """

    id = "gmail.new-email"
    title = "New email"
    Manual = InboxChoice

    async def poll(
        self, ctx: TriggerContext, params: EmailFilter, cursor: str | None
    ) -> Poll[Email]:
        """Return the matching emails that came since the cursor."""
        if not await ensure_authenticated(ctx.user):
            # The user was sent a link. The cursor stays, so that the
            # emails of the time without a login are found after it.
            return Poll(events=[])
        started = _now()
        try:
            since = datetime.datetime.fromisoformat(cursor or "") - OVERLAP
        except ValueError:
            since = started - FIRST_LOOK_BACK
        gmail = await GmailApi.for_user(ctx.user)
        emails = await gmail.get_messages_after(since)
        # Gmail gives the newest first. Run for the oldest first.
        matching = [e for e in reversed(emails) if params.matches(e)]
        return Poll(
            events=[email_event(e) for e in matching], cursor=started.isoformat()
        )

    def key(self, event: Email) -> str:
        """Return the id of the email."""
        return event.id

    async def manual_events(
        self, ctx: TriggerContext, params: EmailFilter, answer: InboxChoice
    ) -> list[Email]:
        """Return the new emails, or the ones in the inbox, that match."""
        if answer.which == "New emails":
            return await self.new_events(ctx, params)
        if not await ensure_authenticated(ctx.user):
            return []
        query = "in:inbox"
        if answer.which == "Starred inbox emails":
            query += " is:starred"
        gmail = await GmailApi.for_user(ctx.user)
        emails = await gmail.get_recent_messages(query=query, max_results=INBOX_LIMIT)
        return [email_event(e) for e in reversed(emails) if params.matches(e)]


@dataclass
class GetAttachmentsInput:
    """Which attachments of which email."""

    # The id of the email: ``{{ trigger.id }}``.
    email: str
    types: str = field(
        default="application/pdf",
        metadata={
            "config_help": "MIME types, with commas between them. Empty: "
            "every attachment."
        },
    )


@dataclass
class GetAttachmentsOutput:
    """The attachments, as files."""

    files: list[File] = described(
        "The attachments, as files. A loop over them writes or sends each",
    )
    count: int = described("How many attachments there are")


class GetAttachments(Action[GetAttachmentsInput, GetAttachmentsOutput]):
    """Get the files that are attached to an email."""

    id = "gmail.get-attachments"
    title = "Get attachments"

    async def run(
        self, ctx: StepContext, inputs: GetAttachmentsInput
    ) -> GetAttachmentsOutput:
        """Fetch the attachments of the given types."""
        types = [t.strip() for t in inputs.types.split(",") if t.strip()]
        gmail = await GmailApi.for_user(ctx.user)
        attachments = await gmail.get_attachments(
            inputs.email, mime_types=types or None
        )
        files = [
            file_of(a.filename or f"attachment-{number}", a.mime_type, a.data)
            for number, a in enumerate(attachments, start=1)
        ]
        return GetAttachmentsOutput(files=files, count=len(files))


@dataclass
class FindLinksInput:
    """Which links of a text."""

    # The text to look in: ``{{ trigger.body }}``.
    text: str
    url: str = field(
        default="",
        metadata={"config_help": "A regular expression for the link. Empty: any."},
    )
    near: str = field(
        default="",
        metadata={
            "config_help": "A regular expression for the text within 100 "
            "characters of the link. Empty: any."
        },
    )
    limit: int = 3


@dataclass
class FindLinksOutput:
    """The links that were found, in the order of the text."""

    links: list[str] = described(
        "The links that were found, in the order of the text",
    )
    count: int = described("How many links were found")


class FindLinks(Action[FindLinksInput, FindLinksOutput]):
    """Find the links in a text, such as the body of an email."""

    id = "gmail.find-links"
    title = "Find links"

    async def run(self, ctx: StepContext, inputs: FindLinksInput) -> FindLinksOutput:
        """Return the first links that match, up to the limit."""
        links = find_links(inputs.text, inputs.url, inputs.near)[: max(inputs.limit, 0)]
        return FindLinksOutput(links=links, count=len(links))


def find_links(text: str, url_regex: str = "", near_regex: str = "") -> list[str]:
    """Return the http(s) links of a text that match, in its order.

    ``url_regex`` is searched in the link. ``near_regex`` is searched in
    the text from 100 characters before the link to 100 after it. Both are
    without regard to case, and an empty one matches.
    """
    links = _ANY_URL_RE.findall(text)
    if url_regex:
        pattern = re.compile(url_regex, re.IGNORECASE)
        links = [link for link in links if pattern.search(link)]
    if not near_regex:
        return links
    around = re.compile(near_regex, re.IGNORECASE)
    found = []
    for link in links:
        at = text.find(link)
        if at != -1 and around.search(text[max(0, at - 100) : at + len(link) + 100]):
            found.append(link)
    return found


def gmail_blocks() -> list[Block]:
    """Return the blocks that the Gmail plugin publishes."""
    return [NewEmail(), GetAttachments(), FindLinks()]
