"""Gmail from end to end: a poll, a workflow made of the Gmail blocks, the vault.

The engine, the trigger and the blocks are the real ones. Redis is in
memory, and Gmail and Obsidian are mocks.
"""

import asyncio
import contextlib
import datetime
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.apis.gmail import Attachment, EmailMessage
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.gmail_monitor import GmailMonitorPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, TriggerSpec, Workflow
from spanreed.workflows.testing import Polls

PDF = b"%PDF-1.7 receipt"


def _receipts() -> Workflow:
    """Return a workflow that saves the PDFs of receipts into the vault."""
    save = Step(
        id="save",
        block="obsidian.write-file",
        inputs={
            "path": "Receipts/{{ trigger.date }} {{ each.item.name }}",
            "content": "{{ each.item.data }}",
            "if_exists": "skip",
        },
    )
    told = Step(
        id="told",
        block="core.send-message",
        inputs={
            "message": "{% if save.written %}💾 Saved {{ save.path }}"
            "{% else %}⚠️ {{ save.path }} already exists; skipped.{% endif %}"
        },
    )
    return Workflow(
        id="wf_receipts",
        user_id=4,
        name="Receipts → vault",
        trigger=TriggerSpec(
            block="gmail.new-email",
            params={"sender": r"billing@shop\.example", "attachments": "yes"},
        ),
        steps=[
            Step(
                id="files",
                block="gmail.get-attachments",
                inputs={"email": "{{ trigger.id }}"},
            ),
            Step(
                id="no_files",
                block="core.send-message",
                inputs={
                    "message": "{% if files.count == 0 %}📎 No matching attachment "
                    "to save from {{ trigger.sender }}{% endif %}"
                },
            ),
            Step(
                id="each",
                block="core.for-each",
                inputs={"items": "{{ files.files }}"},
                steps=[save, told],
            ),
        ],
    )


def _message(message_id: str, **fields: Any) -> EmailMessage:
    values: dict[str, Any] = {
        "id": message_id,
        "thread_id": "t",
        "sender": "Shop <billing@shop.example>",
        "subject": "Your receipt",
        "body": "Thanks.",
        "snippet": "Thanks.",
        "date": datetime.datetime(2026, 5, 9, 9, 30),
        "labels": [],
        "has_attachments": True,
    }
    values.update(fields)
    return EmailMessage(**values)


@dataclass
class World:
    """What a test works with."""

    engine: WorkflowEngine
    gmail: MagicMock
    obsidian: AsyncMock
    bot: AsyncMock
    polls: Polls
    # The emails that Gmail has, newest first, and the attachments of each.
    emails: list[EmailMessage] = field(default_factory=list)
    attachments: dict[str, list[Attachment]] = field(default_factory=dict)
    # The paths that the vault has a file at, and the ones among them that
    # a write found there and left.
    vault: set[str] = field(default_factory=set)
    existed: set[str] = field(default_factory=set)

    def written(self) -> dict[str, bytes]:
        """Return the data of each file that was written, by path."""
        calls = self.obsidian.write_binary_file.await_args_list
        return {c.args[0]: c.args[1] for c in calls if c.args[0] not in self.existed}

    def said(self) -> list[str]:
        return [c.args[0] for c in self.bot.send_message.await_args_list]

    async def statuses(self) -> list[str]:
        runs = await self.engine.store.recent_runs("wf_receipts", 100)
        return [run["status"] for run in reversed(runs)]


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), GmailMonitorPlugin()]
    )
    polls = Polls()
    gmail = MagicMock()
    gmail.is_authenticated = AsyncMock(return_value=True)
    with (
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch(
            "spanreed.workflows.engine.User.find_by_id",
            new=AsyncMock(side_effect=mock_user_find_by_id),
        ),
        patch.object(Plugin, "is_registered", async_return_true),
        patch("spanreed.apis.gmail_blocks.GmailApi") as gmail_cls,
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as obsidian,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.builtin.UserInteractionApi", new=bot),
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
        patch("spanreed.workflows.blocks._sleep", new=polls.sleep),
    ):
        gmail_cls.for_user = AsyncMock(return_value=gmail)
        world = World(
            engine=WorkflowEngine(registry),
            gmail=gmail,
            obsidian=obsidian,
            bot=bot,
            polls=polls,
        )

        async def messages_after(moment: Any) -> list[EmailMessage]:
            return list(world.emails)

        async def attachments(email_id: str, mime_types: Any) -> list[Attachment]:
            return list(world.attachments.get(email_id, []))

        async def write(path: str, data: bytes, overwrite: bool) -> None:
            if path in world.vault and not overwrite:
                world.existed.add(path)
                raise FileExistsError(path)
            world.vault.add(path)

        gmail.get_messages_after = AsyncMock(side_effect=messages_after)
        gmail.get_attachments = AsyncMock(side_effect=attachments)
        obsidian.for_user = AsyncMock(return_value=obsidian)
        obsidian.write_binary_file = AsyncMock(side_effect=write)
        bot.for_user = AsyncMock(return_value=bot)
        for name in ("send_message", "notify"):
            setattr(bot, name, AsyncMock())
        yield world


def _run(scenario: Any) -> None:
    async def with_engine(world: World) -> None:
        try:
            await scenario(world)
        finally:
            await world.engine.stop()

    with _world() as world:
        asyncio.run(with_engine(world))


def test_the_workflow_fits_the_blocks() -> None:
    with _world() as world:
        assert world.engine.problems(_receipts()) == []


def test_the_attachments_of_a_matching_email_are_saved_and_announced() -> None:
    async def scenario(world: World) -> None:
        world.emails = [
            _message("m2", sender="News <news@example.com>"),
            _message("m1"),
        ]
        world.attachments["m1"] = [
            Attachment("receipt.pdf", "application/pdf", PDF),
            Attachment("terms: v2.pdf", "application/pdf", b"%PDF-terms"),
        ]

        await world.engine.save(_receipts())
        await world.polls.over()

        # Only the email of the shop ran, and each attachment is a file.
        assert await world.statuses() == ["finished"]
        assert world.written() == {
            "Receipts/2026-05-09 receipt.pdf": PDF,
            "Receipts/2026-05-09 terms v2.pdf": b"%PDF-terms",
        }
        assert world.said() == [
            "💾 Saved Receipts/2026-05-09 receipt.pdf",
            "💾 Saved Receipts/2026-05-09 terms v2.pdf",
        ]
        # The data of a file is not kept in the trace: only its size.
        (run,) = await world.engine.store.recent_runs("wf_receipts", 1)
        assert run["steps"][0]["outputs"]["files"][0]["data"] == f"<{len(PDF)} bytes>"

    _run(scenario)


def test_an_email_is_handled_once_however_often_gmail_returns_it() -> None:
    async def scenario(world: World) -> None:
        world.emails = [_message("m1")]
        world.attachments["m1"] = [Attachment("receipt.pdf", "application/pdf", PDF)]
        await world.engine.save(_receipts())

        await world.polls.again()
        await world.polls.again()

        assert await world.statuses() == ["finished"]
        assert len(world.said()) == 1

    _run(scenario)


def test_a_file_that_is_there_is_left_and_the_user_is_told() -> None:
    async def scenario(world: World) -> None:
        world.emails = [_message("m1")]
        world.attachments["m1"] = [Attachment("receipt.pdf", "application/pdf", PDF)]
        world.vault.add("Receipts/2026-05-09 receipt.pdf")

        await world.engine.save(_receipts())
        await world.polls.over()

        assert world.written() == {}
        assert world.said() == [
            "⚠️ Receipts/2026-05-09 receipt.pdf already exists; skipped."
        ]
        assert await world.statuses() == ["finished"]

    _run(scenario)


def test_an_email_with_no_attachment_of_the_type_says_so() -> None:
    async def scenario(world: World) -> None:
        world.emails = [_message("m1")]

        await world.engine.save(_receipts())
        await world.polls.over()

        assert world.said() == [
            "📎 No matching attachment to save from Shop <billing@shop.example>"
        ]
        world.obsidian.write_binary_file.assert_not_called()

    _run(scenario)


def test_an_email_that_could_not_be_saved_is_found_again_by_the_next_poll() -> None:
    async def scenario(world: World) -> None:
        world.emails = [_message("m1")]
        world.attachments["m1"] = [Attachment("receipt.pdf", "application/pdf", PDF)]
        attempts = WorkflowEngine.MAX_ATTEMPTS
        world.obsidian.write_binary_file.side_effect = [
            RuntimeError("Obsidian did not answer")
        ] * attempts + [None]

        await world.engine.save(_receipts())
        await world.polls.over()
        assert await world.statuses() == ["failed"] * attempts

        await world.polls.again()

        assert await world.statuses() == ["failed"] * attempts + ["finished"]
        assert world.said() == ["💾 Saved Receipts/2026-05-09 receipt.pdf"]

    _run(scenario)
