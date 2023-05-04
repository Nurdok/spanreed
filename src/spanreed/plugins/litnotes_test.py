import re
import urllib.parse
import asyncio
import base64
import datetime
import textwrap
from unittest.mock import MagicMock, patch, AsyncMock, call
from typing import Any

from spanreed.user import User
from spanreed.plugins.litnotes import LitNotesPlugin, UserConfig
from spanreed.plugin import Plugin
from spanreed.apis.google_books import Book, GoogleBooks
from spanreed.apis.obsidian_webhook import ObsidianWebhookPlugin
from spanreed.test_utils import (
    patch_redis,
    mock_user_find_by_id,
    patch_telegram_bot,
    async_return_false,
    async_return_true,
)


def test_name() -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()

    assert plugin.name() == "Lit Notes"
    assert plugin.canonical_name() == "lit-notes"


@patch_redis
def test_get_users(mock_redis: MagicMock) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()

    with patch.object(
        User,
        "find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        mock_redis.smembers.return_value = {b"4", b"7"}
        users: list[User] = asyncio.run(plugin.get_users())
        assert len(users) == 2
        assert set(u.id for u in users) == {4, 7}


@patch_telegram_bot("spanreed.plugins.litnotes")
def test_ask_for_user_config(mock_bot: AsyncMock) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()

    with patch.object(
        User,
        "find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        mock_bot.request_user_choice.return_value = 0  # "Yes"
        mock_bot.request_user_input.side_effect = [
            "vault name",
            "file location",
            "note title template",
            "note content template",
        ]
        mock_user4 = asyncio.run(User.find_by_id(4))
        mock_set_config = AsyncMock(name="set_config")

        with patch.object(
            LitNotesPlugin,
            "set_config",
            new=mock_set_config,
        ):
            asyncio.run(plugin.ask_for_user_config(mock_user4))

            assert mock_set_config.call_count == 1
            assert mock_set_config.call_args_list[0] == call(
                mock_user4,
                UserConfig(
                    vault="vault name",
                    file_location="file location",
                    note_title_template="note title template",
                    note_content_template="note content template",
                ),
            )


@patch.object(ObsidianWebhookPlugin, "is_registered", async_return_false)
@patch.object(GoogleBooks, "get_books")
@patch.object(LitNotesPlugin, "get_config")
@patch_telegram_bot("spanreed.plugins.litnotes")
def test_ask_for_book_note(
    mock_bot: AsyncMock, mock_get_config: AsyncMock, mock_gbooks: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()

    book: Book = Book(
        title="Neverwhere",
        authors=["Neil Gaiman"],
        publisher="Harper Collins",
        publication_date=datetime.datetime(2003, 6, 17),
        description="bla",
        thumbnail_url="https://example.com/neverwhere.jpg",
    )
    mock_gbooks.return_value = [book]

    mock_user = mock_user_find_by_id(4)

    mock_get_config.return_value = UserConfig(
        vault="v",
        file_location="dir/",
        note_title_template="{{ book.short_title }}",
        note_content_template=(
            "{{ free_text }}\n"
            "{% for r in recommended_by %}{{ r }}{% endfor %}\n"
            "{{ book.formatted_authors }}\n"
            "{{ book.publication_year }}\n"
            "{{ book.thumbnail_url }}\n"
        ),
    )

    def fake_user_input(prompt: str) -> str:
        if "Which book" in prompt:
            return "Neverwhere"

        if "Who recommended" in prompt:
            return "me"

        if "free text" in prompt:
            return "free"

        assert False, f"Unexpected prompt: {prompt}"

    mock_bot.request_user_input.side_effect = fake_user_input

    def fake_user_choice(prompt: str, choices: list[str]) -> int:
        if "Found one book" in prompt:
            assert choices == ["Yes", "No"]
            return 0

        if "Recommended" in prompt:
            assert choices == ["Yes", "No"]
            if "another" in prompt:
                return 1
            return 0

        if "free text" in prompt:
            assert choices == ["Yes", "No"]
            return 0

        assert False, f"Unexpected prompt: {prompt}"

    mock_bot.request_user_choice.side_effect = fake_user_choice
    asyncio.run(plugin._ask_for_book_note(mock_user))
    html_msg: str = mock_bot.send_message.call_args.kwargs["text"]
    assert "amir.rachum.com/fwdr" in html_msg
    if (link := re.search(r'url=([^"]+)"', html_msg)) is None:
        assert False, f"Unexpected message: {html_msg}"
    obsidian_uri_params = urllib.parse.parse_qs(
        urllib.parse.urlparse(
            base64.urlsafe_b64decode(link.group(1)).decode("utf-8")
        ).query
    )

    assert obsidian_uri_params["vault"] == ["v"]
    assert obsidian_uri_params["file"] == ["dir/Neverwhere"]
    assert obsidian_uri_params["content"] == [
        textwrap.dedent(
            """\
                free
                me
                [[Neil Gaiman]]
                2003
                https://example.com/neverwhere.jpg"""
        )
    ]


@patch.object(ObsidianWebhookPlugin, "is_registered", async_return_true)
@patch("spanreed.plugins.litnotes.ObsidianWebhookApi", autospec=True)
@patch.object(GoogleBooks, "get_books")
@patch.object(LitNotesPlugin, "get_config")
@patch_telegram_bot("spanreed.plugins.litnotes")
def test_ask_for_book_note_with_webhooks(
    mock_bot: AsyncMock,
    mock_get_config: AsyncMock,
    mock_gbooks: AsyncMock,
    mock_webhook_api: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()

    book: Book = Book(
        title="Neverwhere",
        authors=["Neil Gaiman"],
        publisher="Harper Collins",
        publication_date=datetime.datetime(2003, 6, 17),
        description="bla",
        thumbnail_url="https://example.com/neverwhere.jpg",
    )
    mock_gbooks.return_value = [book]

    mock_user = mock_user_find_by_id(4)

    mock_get_config.return_value = UserConfig(
        vault="v",
        file_location="dir/",
        note_title_template="{{ book.short_title }}",
        note_content_template=(
            "{{ free_text }}\n"
            "{% for r in recommended_by %}{{ r }}{% endfor %}\n"
            "{{ book.formatted_authors }}\n"
            "{{ book.publication_year }}\n"
            "{{ book.thumbnail_url }}\n"
        ),
    )

    def fake_user_input(prompt: str) -> str:
        if "Which book" in prompt:
            return "Neverwhere"

        if "Who recommended" in prompt:
            return "me"

        if "free text" in prompt:
            return "free"

        assert False, f"Unexpected prompt: {prompt}"

    mock_bot.request_user_input.side_effect = fake_user_input

    def fake_user_choice(prompt: str, choices: list[str]) -> int:
        if "Found one book" in prompt:
            assert choices == ["Yes", "No"]
            return 0

        if "Recommended" in prompt:
            assert choices == ["Yes", "No"]
            if "another" in prompt:
                return 1
            return 0

        if "free text" in prompt:
            assert choices == ["Yes", "No"]
            return 0

        assert False, f"Unexpected prompt: {prompt}"

    mock_bot.request_user_choice.side_effect = fake_user_choice
    mock_webhook_api.for_user = AsyncMock(return_value=mock_webhook_api)

    asyncio.run(plugin._ask_for_book_note(mock_user))

    mock_webhook_api.append_to_note.assert_called_once_with(
        "dir/Neverwhere.md",
        textwrap.dedent(
            """\
                free
                me
                [[Neil Gaiman]]
                2003
                https://example.com/neverwhere.jpg"""
        ),
    )
