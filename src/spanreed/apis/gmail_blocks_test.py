import asyncio
import datetime
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.gmail import Attachment, EmailMessage
from spanreed.apis.gmail_blocks import (
    Email,
    EmailFilter,
    FindLinks,
    FindLinksInput,
    GetAttachments,
    GetAttachmentsInput,
    InboxChoice,
    NewEmail,
    email_event,
    find_links,
    gmail_blocks,
)
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.blocks import (
    BlockRegistry,
    RunInfo,
    StepContext,
    TriggerContext,
)
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.testing import fake_plugin
from spanreed.workflows.values import from_data

NOW = datetime.datetime(2026, 5, 9, 12, 0, tzinfo=datetime.timezone.utc)


class _State:
    """A trigger's state, kept in a dict."""

    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


def _trigger_context(**state: str) -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=_State(**state),
        queue=WorkflowQueue("wf_1"),
    )


def _step_context() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="r1", manual=False),
    )


def _message(message_id: str = "m1", **fields: Any) -> EmailMessage:
    values: dict[str, Any] = {
        "id": message_id,
        "thread_id": "t1",
        "sender": "Shop <billing@shop.example>",
        "subject": "Your receipt #123",
        "body": "Thanks for your order.",
        "snippet": "Thanks for",
        "date": datetime.datetime(2026, 5, 9, 9, 30, 5),
        "labels": [],
        "has_attachments": True,
    }
    values.update(fields)
    return EmailMessage(**values)


@pytest.fixture(autouse=True)
def gmail() -> Iterator[MagicMock]:
    """Stand in for the Gmail API of a user who is logged in."""
    api = MagicMock()
    api.is_authenticated = AsyncMock(return_value=True)
    api.get_messages_after = AsyncMock(return_value=[])
    api.get_recent_messages = AsyncMock(return_value=[])
    api.get_attachments = AsyncMock(return_value=[])
    with (
        patch("spanreed.apis.gmail_blocks.GmailApi") as cls,
        patch("spanreed.apis.gmail_blocks._now", new=lambda: NOW),
    ):
        cls.for_user = AsyncMock(return_value=api)
        cls.is_app_configured = AsyncMock(return_value=True)
        api.cls = cls
        yield api


def test_the_gmail_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in gmail_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == [
        "gmail.new-email",
        "gmail.get-attachments",
        "gmail.find-links",
    ]


# -- the filter ----------------------------------------------------------------


def test_an_empty_filter_matches_every_email() -> None:
    assert EmailFilter().matches(_message())
    assert EmailFilter().interval == datetime.timedelta(minutes=15)


def test_each_part_of_the_filter_is_a_pattern_without_regard_to_case() -> None:
    email = _message()

    assert EmailFilter(sender=r"BILLING@shop\.example").matches(email)
    assert EmailFilter(subject=r"receipt #\d+", body="order").matches(email)
    assert not EmailFilter(sender="bank").matches(email)
    assert not EmailFilter(subject="receipt", body="refund").matches(email)


def test_the_filter_can_ask_for_attachments_or_for_none() -> None:
    with_file, without = _message(), _message(has_attachments=False)

    assert EmailFilter(attachments="yes").matches(with_file)
    assert not EmailFilter(attachments="yes").matches(without)
    assert EmailFilter(attachments="no").matches(without)
    assert not EmailFilter(attachments="no").matches(with_file)


def test_a_pattern_that_is_no_regular_expression_is_refused() -> None:
    with pytest.raises(ValueError, match="Subject: this is not a regular expression"):
        from_data(EmailFilter, {"subject": "receipt ("})
    with pytest.raises(ValueError):
        from_data(EmailFilter, {"every": 0})


# -- an email as an event ------------------------------------------------------


def test_an_email_gives_its_fields_and_names_that_are_safe_in_a_file_name() -> None:
    email = _message(subject='Re: "Invoice" 5/2026 — please pay')

    assert email_event(email) == Email(
        id="m1",
        sender="Shop <billing@shop.example>",
        # Up to the @, without what a file name cannot hold.
        sender_name="Shop billing",
        subject='Re: "Invoice" 5/2026 — please pay',
        subject_name="Re_ _Invoice_ 5_2026 _ please pay",
        date="2026-05-09",
        received="2026-05-09 09:30:05",
        snippet="Thanks for",
        body="Thanks for your order.",
        has_attachments=True,
    )
    assert len(email_event(_message(subject="x" * 80)).subject_name) == 50


# -- polls ---------------------------------------------------------------------


def _poll(ctx: TriggerContext, cursor: str | None = None, **filter_: Any) -> Any:
    return asyncio.run(NewEmail().poll(ctx, EmailFilter(**filter_), cursor))


def test_the_first_poll_looks_back_an_hour(gmail: MagicMock) -> None:
    poll = _poll(_trigger_context())

    gmail.get_messages_after.assert_awaited_once_with(NOW - datetime.timedelta(hours=1))
    assert poll.events == []
    # The next poll starts at the time this one was made.
    assert poll.cursor == NOW.isoformat()


def test_a_later_poll_asks_from_a_little_before_the_cursor(gmail: MagicMock) -> None:
    at_ten = datetime.datetime(2026, 5, 9, 10, 0, tzinfo=datetime.timezone.utc)

    _poll(_trigger_context(), cursor=at_ten.isoformat())

    # An email can reach Gmail a little after the time it carries.
    gmail.get_messages_after.assert_awaited_once_with(
        at_ten - datetime.timedelta(hours=1)
    )


def test_a_poll_gives_the_matching_emails_oldest_first(gmail: MagicMock) -> None:
    # Gmail gives the newest first.
    gmail.get_messages_after.return_value = [
        _message("m3", subject="Receipt 3"),
        _message("m2", subject="Newsletter"),
        _message("m1", subject="Receipt 1"),
    ]

    poll = _poll(_trigger_context(), subject="receipt")

    assert [(e.id, e.subject) for e in poll.events] == [
        ("m1", "Receipt 1"),
        ("m3", "Receipt 3"),
    ]
    assert NewEmail().key(poll.events[0]) == "m1"


def test_without_a_login_a_poll_sends_a_link_and_keeps_its_place(
    gmail: MagicMock,
) -> None:
    gmail.is_authenticated.return_value = False
    flow = MagicMock()
    flow.get_done_event = AsyncMock()
    flow.get_auth_url = AsyncMock(return_value="https://accounts.example/auth")
    gmail.cls.start_authentication = AsyncMock(return_value=flow)

    with patch("spanreed.apis.gmail_blocks.UserInteractionApi") as bot_cls:
        bot = AsyncMock()
        bot_cls.for_user = AsyncMock(return_value=bot)
        first = _poll(_trigger_context(), cursor="2026-05-09T10:00:00+00:00")
        # While the link waits to be used, no second one is sent.
        gmail.cls.start_authentication.side_effect = ValueError("already started")
        second = _poll(_trigger_context(), cursor="2026-05-09T10:00:00+00:00")

    bot.send_message.assert_awaited_once()
    assert "https://accounts.example/auth" in bot.send_message.await_args.args[0]
    # No cursor: the emails of the time without a login are found after it.
    assert (first.events, first.cursor) == ([], None)
    assert (second.events, second.cursor) == ([], None)
    gmail.get_messages_after.assert_not_called()


# -- by hand -------------------------------------------------------------------


def _by_hand(which: Any, ctx: TriggerContext | None = None, **filter_: Any) -> Any:
    return asyncio.run(
        NewEmail().manual_events(
            ctx or _trigger_context(), EmailFilter(**filter_), InboxChoice(which=which)
        )
    )


def test_by_hand_new_emails_are_the_ones_the_workflow_did_not_run_for(
    gmail: MagicMock,
) -> None:
    gmail.get_messages_after.return_value = [_message("m2"), _message("m1")]
    ctx = _trigger_context(seen=json.dumps({"m1": ""}))

    assert [e.id for e in _by_hand("New emails", ctx)] == ["m2"]


def test_by_hand_the_inbox_gives_every_match_whether_it_ran_or_not(
    gmail: MagicMock,
) -> None:
    gmail.get_recent_messages.return_value = [
        _message("m2", subject="Receipt 2"),
        _message("m1", subject="Hello"),
    ]
    ctx = _trigger_context(seen=json.dumps({"m2": ""}))

    assert [e.id for e in _by_hand("All inbox emails", ctx, subject="receipt")] == [
        "m2"
    ]
    gmail.get_recent_messages.assert_awaited_once_with(
        query="in:inbox", max_results=200
    )

    _by_hand("Starred inbox emails")
    assert gmail.get_recent_messages.await_args.kwargs["query"] == "in:inbox is:starred"


# -- attachments ---------------------------------------------------------------


def _attachments(**inputs: Any) -> Any:
    values: dict[str, Any] = {"email": "m1"}
    values.update(inputs)
    return asyncio.run(
        GetAttachments().run(_step_context(), GetAttachmentsInput(**values))
    )


def test_get_attachments_gives_the_pdf_files_by_default(gmail: MagicMock) -> None:
    gmail.get_attachments.return_value = [
        Attachment("receipt: May.pdf", "application/pdf", b"%PDF-1"),
        Attachment("", "application/pdf", b"%PDF-2"),
    ]

    output = _attachments()

    gmail.get_attachments.assert_awaited_once_with("m1", mime_types=["application/pdf"])
    assert output.count == 2
    assert [(f.name, f.extension, f.data) for f in output.files] == [
        ("receipt May.pdf", ".pdf", b"%PDF-1"),
        # An attachment with no name is named by its place.
        ("attachment-2", "", b"%PDF-2"),
    ]


def test_get_attachments_takes_types_with_commas_and_none_for_all(
    gmail: MagicMock,
) -> None:
    _attachments(types=" image/png , application/zip,")
    assert gmail.get_attachments.await_args.kwargs == {
        "mime_types": ["image/png", "application/zip"]
    }

    assert _attachments(types="").count == 0
    assert gmail.get_attachments.await_args.kwargs == {"mime_types": None}


# -- links ---------------------------------------------------------------------

BODY = (
    "Hello! Your invoice is ready: https://pay.example/invoice/7?t=abc "
    + "x" * 150
    + " Unsubscribe here: https://news.example/unsubscribe "
    + "and see https://pay.example/terms."
)


def test_find_links_gives_the_links_in_the_order_of_the_text() -> None:
    assert find_links(BODY) == [
        "https://pay.example/invoice/7?t=abc",
        "https://news.example/unsubscribe",
        "https://pay.example/terms.",
    ]
    assert find_links("No links here.") == []


def test_find_links_can_ask_for_the_link_and_for_the_text_around_it() -> None:
    assert find_links(BODY, url_regex=r"PAY\.example") == [
        "https://pay.example/invoice/7?t=abc",
        "https://pay.example/terms.",
    ]
    # Within 100 characters of the link.
    assert find_links(BODY, near_regex="invoice is ready") == [
        "https://pay.example/invoice/7?t=abc"
    ]
    assert find_links(BODY, url_regex="pay", near_regex="unsubscribe") == [
        "https://pay.example/terms."
    ]


def test_the_block_gives_the_first_links_up_to_its_limit() -> None:
    def run(**inputs: Any) -> Any:
        return asyncio.run(
            FindLinks().run(_step_context(), FindLinksInput(text=BODY, **inputs))
        )

    assert run().count == 3
    limited = run(limit=1, url="example")
    assert (limited.links, limited.count) == (
        ["https://pay.example/invoice/7?t=abc"],
        1,
    )
    assert run(limit=0).links == []
