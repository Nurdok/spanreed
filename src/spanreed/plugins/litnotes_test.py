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
from spanreed.apis.obsidian import ObsidianApiTimeoutError, ObsidianPlugin
from spanreed.test_utils import (
    patch_redis,
    mock_user_find_by_id,
    patch_user_interaction,
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


@patch_user_interaction("spanreed.plugins.litnotes")
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


NOTE_CONTENT = textwrap.dedent(
    """\
        free
        me
        [[Neil Gaiman]]
        2003
        https://example.com/neverwhere.jpg"""
)


def _set_up_one_book(
    mock_bot: AsyncMock, mock_get_config: AsyncMock, mock_gbooks: AsyncMock
) -> None:
    """Make "Add book note" find one book and answer all its questions.

    The note then has the title ``Neverwhere`` and the content
    ``NOTE_CONTENT``.
    """
    mock_gbooks.return_value = [
        Book(
            title="Neverwhere",
            authors=["Neil Gaiman"],
            publisher="Harper Collins",
            publication_date=datetime.datetime(2003, 6, 17),
            description="bla",
            thumbnail_url="https://example.com/neverwhere.jpg",
        )
    ]

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


def _assert_sent_link_creates_the_note(mock_bot: AsyncMock) -> None:
    """Check that the last message is the link that creates the note."""
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
    assert obsidian_uri_params["content"] == [NOTE_CONTENT]


@patch.object(ObsidianPlugin, "is_registered", async_return_false)
@patch.object(GoogleBooks, "get_books")
@patch.object(LitNotesPlugin, "get_config")
@patch_user_interaction("spanreed.plugins.litnotes")
def test_ask_for_book_note(
    mock_bot: AsyncMock, mock_get_config: AsyncMock, mock_gbooks: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()
    _set_up_one_book(mock_bot, mock_get_config, mock_gbooks)

    asyncio.run(plugin._ask_for_book_note(mock_user_find_by_id(4)))

    _assert_sent_link_creates_the_note(mock_bot)


@patch.object(ObsidianPlugin, "is_registered", async_return_true)
@patch("spanreed.plugins.litnotes.ObsidianApi", autospec=True)
@patch.object(GoogleBooks, "get_books")
@patch.object(LitNotesPlugin, "get_config")
@patch_user_interaction("spanreed.plugins.litnotes")
def test_ask_for_book_note_in_vault(
    mock_bot: AsyncMock,
    mock_get_config: AsyncMock,
    mock_gbooks: AsyncMock,
    mock_obsidian_api: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()
    _set_up_one_book(mock_bot, mock_get_config, mock_gbooks)
    mock_obsidian_api.for_user = AsyncMock(return_value=mock_obsidian_api)
    mock_obsidian_api.append_to_note = AsyncMock()

    asyncio.run(plugin._ask_for_book_note(mock_user_find_by_id(4)))

    mock_obsidian_api.append_to_note.assert_awaited_once_with(
        "dir/Neverwhere.md", NOTE_CONTENT
    )
    mock_bot.send_message.assert_awaited_once_with("Note created in Obsidian!")


@patch.object(ObsidianPlugin, "is_registered", async_return_true)
@patch("spanreed.plugins.litnotes.ObsidianApi", autospec=True)
@patch.object(GoogleBooks, "get_books")
@patch.object(LitNotesPlugin, "get_config")
@patch_user_interaction("spanreed.plugins.litnotes")
def test_ask_for_book_note_sends_link_when_obsidian_is_offline(
    mock_bot: AsyncMock,
    mock_get_config: AsyncMock,
    mock_gbooks: AsyncMock,
    mock_obsidian_api: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()
    _set_up_one_book(mock_bot, mock_get_config, mock_gbooks)
    mock_obsidian_api.for_user = AsyncMock(return_value=mock_obsidian_api)
    mock_obsidian_api.append_to_note = AsyncMock(side_effect=ObsidianApiTimeoutError())

    asyncio.run(plugin._ask_for_book_note(mock_user_find_by_id(4)))

    # What the user entered is not lost: the link carries the same note.
    assert "not created" in mock_bot.send_message.call_args_list[0].args[0]
    _assert_sent_link_creates_the_note(mock_bot)
