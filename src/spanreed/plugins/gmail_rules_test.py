"""A rule of the old Gmail Monitor, as a workflow, does what the rule did.

The workflows are run by the real engine with the real blocks. Gmail, the
web and Obsidian are mocks. ``gmail_rules_test_data`` holds what the rule
actions said and wrote for the same email before they were removed.
"""

import asyncio
import contextlib
import datetime
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spanreed.apis.gmail import Attachment, EmailMessage
from spanreed.apis.gmail_blocks import email_event
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.plugin import Plugin
from spanreed.plugins.gmail_monitor import GmailMonitorPlugin
from spanreed.plugins.gmail_rules import steps_for, workflow_for_rule
from spanreed.plugins.gmail_rules_test_data import (
    ATTACHMENTS,
    CASES,
    DOWNLOADS,
    EMAIL,
    RULE_NAME,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.files import NotAFile, _Response
from spanreed.workflows.model import Workflow


def _email() -> EmailMessage:
    return EmailMessage(**{**EMAIL, "date": datetime.datetime(*EMAIL["date"])})


def _rule(*actions: tuple[str, dict[str, Any]], **fields: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "name": RULE_NAME,
        "filter": {},
        "actions": [{"type": kind, "config": config} for kind, config in actions],
        "enabled": True,
    }
    rule.update(fields)
    return rule


def _workflow(*actions: tuple[str, dict[str, Any]], **fields: Any) -> Workflow:
    return workflow_for_rule("wf_rule", 4, _rule(*actions, **fields), 15, 1)


@dataclass
class Ran:
    """What a run of a workflow said, sent and wrote."""

    status: str
    messages: list[str]
    # The name and the data of each file that was sent to the user.
    documents: list[tuple[str, bytes]]
    # Each path that was written, and whether a file there was replaced.
    written: list[list[Any]]
    # The types that the attachments were asked for with.
    types: list[Any] = field(default_factory=list)


@contextlib.contextmanager
def _engine() -> Iterator[WorkflowEngine]:
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), GmailMonitorPlugin()]
    )
    with (
        patch("spanreed.workflows.store.redis_api", new=FakeRedis()),
        patch.object(Plugin, "is_registered", async_return_true),
    ):
        yield WorkflowEngine(registry)


def _run(
    workflow: Workflow,
    attachments: Any = ATTACHMENTS,
    exists: tuple[str, ...] = (),
    downloads: Any = None,
) -> Ran:
    """Run a workflow once for the sample email."""
    downloads = DOWNLOADS if downloads is None else downloads
    gmail = MagicMock()
    gmail.get_attachments = AsyncMock(
        return_value=[Attachment(*attachment) for attachment in attachments]
    )

    async def download(url: str, max_size_mb: float) -> Any:
        answer = downloads.get(url)
        if isinstance(answer, Exception):
            raise answer
        name, content_type = answer
        response = _Response(
            status=200,
            reason="OK",
            url=url,
            headers={"content-type": content_type},
            data=b"%PDF-" + url.encode(),
        )
        return response, name

    async def write(path: str, data: bytes, overwrite: bool = False) -> None:
        if path in exists and not overwrite:
            raise FileExistsError(path)

    async def scenario(engine: WorkflowEngine) -> dict[str, Any]:
        assert engine.problems(workflow) == []
        return await engine._run(
            workflow,
            mock_user_find_by_id(4),
            email_event(_email()),
            manual=False,
            attempt=1,
        )

    bot = AsyncMock()
    obsidian = AsyncMock()
    obsidian.write_binary_file = AsyncMock(side_effect=write)
    with (
        _engine() as engine,
        patch("spanreed.apis.gmail_blocks.GmailApi") as gmail_cls,
        patch("spanreed.apis.obsidian_blocks.ObsidianApi") as obsidian_cls,
        patch("spanreed.workflows.files.download", new=download),
        patch("spanreed.workflows.blocks.UserInteractionApi") as message_bot,
        patch("spanreed.workflows.blocks.UserInteractionApi") as file_bot,
    ):
        gmail_cls.for_user = AsyncMock(return_value=gmail)
        obsidian_cls.for_user = AsyncMock(return_value=obsidian)
        message_bot.for_user = file_bot.for_user = AsyncMock(return_value=bot)
        trace = asyncio.run(scenario(engine))
    return Ran(
        status=trace["status"],
        messages=[call.args[0] for call in bot.send_message.await_args_list],
        documents=[call.args for call in bot.send_document.await_args_list],
        written=[
            [call.args[0], call.kwargs["overwrite"]]
            for call in obsidian.write_binary_file.await_args_list
        ],
        types=[
            call.kwargs["mime_types"] for call in gmail.get_attachments.await_args_list
        ],
    )


# -- what the rule actions did, and the workflow does --------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_rule_as_a_workflow_says_and_writes_what_the_rule_did(name: str) -> None:
    case = CASES[name]
    workflow = _workflow((case["type"], case["config"]))

    ran = _run(
        workflow,
        attachments=[] if name.endswith("_none") else ATTACHMENTS,
        exists=("Receipts/receipt May.pdf",) if name.endswith("_exists") else (),
    )

    assert ran.status == "finished"
    assert ran.messages == case["messages"]
    assert ran.written == case["written"]


def test_the_types_of_attachments_a_rule_asked_for_are_asked_for() -> None:
    config = {"vault_dir": "", "mime_types": ["application/pdf", "image/png"]}
    assert _run(_workflow(("save_attachment_to_vault", config))).types == [
        ["application/pdf", "image/png"]
    ]
    # A rule that asked for every attachment.
    every = _workflow(("download_attachment", {"mime_types": None}))
    assert _run(every).types == [None]
    assert _run(_workflow(("download_attachment", {}))).types == [["application/pdf"]]


# -- where the workflow differs from the rule, on purpose ----------------------


def test_an_attachment_is_sent_under_a_name_that_is_safe_and_never_empty() -> None:
    ran = _run(_workflow(("download_attachment", {})))

    # The rule sent "receipt: May.pdf", and the second file with no name.
    assert ran.documents == [
        ("receipt May.pdf", b"%PDF-1"),
        ("attachment-2", b"%PDF-2"),
    ]
    assert ran.messages == [
        '📄 receipt May.pdf from "Shop: Billing" <billing@shop.example>'
        "\n🏷️ Rule: Receipts & co",
        '📄 attachment-2 from "Shop: Billing" <billing@shop.example>'
        "\n🏷️ Rule: Receipts & co",
    ]


def test_an_email_with_no_attachment_to_send_says_so() -> None:
    ran = _run(_workflow(("download_attachment", {})), attachments=[])

    assert ran.messages == [
        "📎 No matching attachment found in email from "
        '"Shop: Billing" <billing@shop.example>'
    ]
    assert ran.documents == []


def test_the_files_behind_the_links_of_an_email_are_sent() -> None:
    ran = _run(_workflow(("download_link", {"url_regex": "invoice"})))

    # One message for a file. The rule also said which link it was at, how
    # large the file was, and that it was done.
    assert ran.documents == [
        ("invoice-7.pdf", b"%PDF-https://pay.example/invoice/7?t=abc")
    ]
    assert ran.messages == [
        '📄 File from "Shop: Billing" <billing@shop.example>'
        '\n📧 Receipt #1042 / May "2026"\n🏷️ Rule: Receipts & co'
    ]


def test_a_file_from_a_link_can_be_named_as_the_rule_named_it() -> None:
    config = {"custom_filename": "{date}_{rule}_{subject}_{index}.pdf"}

    ran = _run(_workflow(("download_link", config)))

    # The links were counted from 0, and the subject had no spaces.
    assert [name for name, _ in ran.documents] == [
        "2026-05-09_Receipts & co_Receipt__1042___May__2026__0.pdf",
        "2026-05-09_Receipts & co_Receipt__1042___May__2026__1.pdf",
    ]


def test_a_link_that_leads_to_no_file_is_told_and_the_next_link_goes_on() -> None:
    downloads = {
        "https://pay.example/invoice/7?t=abc": NotAFile(
            "The link gave a web page, not a file."
        ),
        "https://pay.example/terms": ("terms.pdf", "application/pdf"),
    }

    sent = _run(_workflow(("download_link", {})), downloads=downloads)
    saved = _run(
        _workflow(("save_link_to_vault", {"vault_dir": "Invoices"})),
        downloads=downloads,
    )

    assert sent.status == saved.status == "finished"
    assert sent.messages[0] == (
        "❌ Failed to download from https://pay.example/invoice/7?t=abc: "
        "The link gave a web page, not a file."
    )
    assert [name for name, _ in sent.documents] == ["terms.pdf"]
    # The rule said "returned text/html, not a file — not saved".
    assert saved.messages == [
        "⚠️ The link for “Receipts & co” was not saved. The link gave a web "
        "page, not a file. Open it manually:\nhttps://pay.example/invoice/7?t=abc",
        "💾 Saved Invoices/terms.pdf (rule: Receipts & co)",
    ]
    assert saved.written == [["Invoices/terms.pdf", False]]


def test_an_email_with_no_link_to_send_says_so() -> None:
    ran = _run(_workflow(("download_link", {"url_regex": "nothing-matches"})))

    assert ran.messages == [
        "🔗 No matching links found in email from "
        '"Shop: Billing" <billing@shop.example>'
    ]


# -- a rule as a workflow ------------------------------------------------------


def test_the_filter_of_a_rule_is_the_filter_of_the_trigger() -> None:
    workflow = _workflow(
        filter={
            "sender_regex": r"billing@shop\.example",
            "subject_regex": None,
            "body_regex": "invoice",
            "has_attachments": True,
        }
    )

    assert workflow.trigger.block == "gmail.new-email"
    assert workflow.trigger.params == {
        "every": 15,
        "unit": "minutes",
        "sender": r"billing@shop\.example",
        "subject": "",
        "body": "invoice",
        "attachments": "yes",
    }
    assert (
        _workflow(filter={"has_attachments": False}).trigger.params["attachments"]
        == "no"
    )
    assert _workflow(filter={}).trigger.params["attachments"] == "any"


def test_a_rule_keeps_its_name_and_whether_it_was_on() -> None:
    on = _workflow()
    off = workflow_for_rule("wf_2", 4, _rule(enabled=False, name=""), 30, 3)

    assert (on.name, on.enabled, on.user_id) == (RULE_NAME, True, 4)
    # The plugin had one command for all rules, so a rule's is hidden.
    assert on.command_hidden
    assert (off.name, off.enabled, off.recipe) == (
        "Email rule 3",
        False,
        "gmail-monitor-rule-3",
    )
    assert off.trigger.params["every"] == 30


def test_the_steps_of_each_action_are_told_apart_by_its_place() -> None:
    workflow = _workflow(
        ("save_attachment_to_vault", {"vault_dir": "Receipts"}),
        ("made_up_action", {}),
        ("telegram_notification", {}),
    )

    # An action of a type the plugin did not know did nothing.
    assert [step.id for step in workflow.steps] == [
        "files_1",
        "no_files_1",
        "each_1",
        "notify_2",
    ]
    assert [step.id for step in workflow.steps[2].steps] == ["save_1", "saved_1"]
    ran = _run(workflow)
    assert ran.status == "finished"
    assert len(ran.messages) == 3 and len(ran.written) == 2


def test_a_rule_with_one_action_has_plain_step_ids() -> None:
    assert [
        step.id for step in steps_for([{"type": "telegram_notification"}], "R")
    ] == ["notify"]
    assert steps_for("not a list", "R") == []


def test_a_number_in_a_file_name_can_be_padded() -> None:
    config = {"vault_dir": "Receipts", "filename_template": "{date} {index:02d}"}

    ran = _run(_workflow(("save_attachment_to_vault", config)))

    assert ran.written == [
        ["Receipts/2026-05-09 01.pdf", False],
        ["Receipts/2026-05-09 02", False],
    ]


def test_a_name_that_holds_template_marks_is_shown_as_it_is() -> None:
    workflow = _workflow(("telegram_notification", {}), name="{{ odd }} rule")

    ran = _run(workflow)

    assert ran.messages[0].startswith("📧 Email matched rule: {{ odd }} rule\n\nFrom:")
