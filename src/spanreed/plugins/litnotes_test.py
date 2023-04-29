import re
import asyncio
import base64
import datetime
import json
import redis.asyncio as redis
from unittest.mock import MagicMock, patch, AsyncMock, call
import logging
from typing import Callable

from spanreed.user import User
from spanreed.plugins.litnotes import LitNotesPlugin, UserConfig
from spanreed.plugin import Plugin
from spanreed.apis.google_books import Book, GoogleBooks


def make_mock_redis() -> MagicMock:
    mock_redis = MagicMock(spec=redis.Redis)
    redis_async_defs = ["get", "set", "smembers", "sadd", "srem", "incr"]
    for def_name in redis_async_defs:
        setattr(mock_redis, def_name, AsyncMock())
    return mock_redis


def mock_user_find_by_id(mock_redis: MagicMock) -> Callable[[int], MagicMock]:
    def f(user_id: int) -> MagicMock:
        mock_user = MagicMock(name=f"user-{user_id}", spec=User)
        mock_user.redis_api = mock_redis
        mock_user.id = user_id
        mock_user.name = "Test User"
        mock_user.plugins = []
        return mock_user

    return f


def test_name() -> None:
    Plugin.reset_registry()

    mock_redis = make_mock_redis()
    litnotes = LitNotesPlugin(redis_api=mock_redis)

    assert litnotes.name() == "Lit Notes"
    assert litnotes.canonical_name() == "lit-notes"


def test_get_users() -> None:
    Plugin.reset_registry()

    mock_redis = make_mock_redis()
    User.redis_api = mock_redis
    litnotes = LitNotesPlugin(redis_api=mock_redis)

    with patch.object(
        User,
        "find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id(mock_redis)),
    ):
        mock_redis.smembers.return_value = {b"4", b"7"}
        users: list[User] = asyncio.run(litnotes.get_users())
        assert len(users) == 2
        assert set(u.id for u in users) == {4, 7}


def test_ask_for_user_config() -> None:
    Plugin.reset_registry()

    mock_redis = make_mock_redis()
    User.redis_api = mock_redis
    litnotes = LitNotesPlugin(redis_api=mock_redis)

    with patch(
        "spanreed.plugins.litnotes.TelegramBotApi", autospec=True
    ) as mock_bot, patch.object(
        User,
        "find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id(mock_redis)),
    ):
        mock_bot.for_user = AsyncMock(return_value=mock_bot)
        mock_bot.request_user_choice = AsyncMock(return_value=0)
        mock_bot.request_user_input = AsyncMock(
            side_effect=[
                "vault name",
                "file location",
                "note title template",
                "note content template",
            ]
        )
        mock_user4 = asyncio.run(User.find_by_id(4))
        mock_set_config = AsyncMock(name="set_config")

        with patch.object(
            LitNotesPlugin,
            "set_config",
            new=mock_set_config,
        ):
            asyncio.run(litnotes.ask_for_user_config(mock_user4))

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


def test_ask_for_book_note() -> None:
    Plugin.reset_registry()

    mock_redis = make_mock_redis()
    User.redis_api = mock_redis
    litnotes = LitNotesPlugin(redis_api=mock_redis)

    book: Book = Book(
        title="Neverwhere",
        authors=["Neil Gaiman"],
        publisher="Harper Collins",
        publication_date=datetime.datetime(2003, 6, 17),
        description="bla",
        thumbnail_url="https://example.com/neverwhere.jpg",
    )

    with patch(
        "spanreed.plugins.litnotes.TelegramBotApi", autospec=True
    ) as mock_bot, patch.object(
        GoogleBooks, "get_books", new=AsyncMock(return_value=[book])
    ) as mock_gbooks, patch.object(
        LitNotesPlugin, "get_config"
    ) as mock_get_config:
        mock_user = mock_user_find_by_id(mock_redis)(4)
        mock_bot.for_user = AsyncMock(return_value=mock_bot)

        mock_get_config.return_value = UserConfig(
            vault="v",
            file_location="dir/",
            note_title_template="{{ book.short_title }}",
            note_content_template=(
                "{{ free_text }}:{% for r in "
                "recommended_by %}{{ r }}{% endfor %}"
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
        asyncio.run(litnotes._ask_for_book_note(mock_user))
        html_msg: str = mock_bot.send_message.call_args.kwargs["text"]
        assert "amir.rachum.com/fwdr" in html_msg
        if (link := re.search(r'url=([^"]+)"', html_msg)) is None:
            assert False, f"Unexpected message: {html_msg}"
        assert (
            base64.urlsafe_b64decode(link.group(1))
            == b"obsidian://new?vault=v&file=dir%2Ffree%3Ame&content=free%3Ame"
        )
