"""Tests of asking Claude, with the API itself stood in for."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx2 as httpx
import pytest

from spanreed.apis.claude import (
    Answer,
    ClaudeApi,
    ClaudeError,
    ClaudePlugin,
    checked_schema,
    file_block,
)
from spanreed.apis.claude_blocks import (
    AskClaude,
    AskClaudeInput,
    AskClaudeOutput,
    claude_blocks,
)
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows import editing
from spanreed.workflows.blocks import BlockRegistry, RunInfo, StepContext
from spanreed.workflows.files import File
from spanreed.workflows.testing import fake_plugin

SCHEMA = {
    "type": "object",
    "properties": {"shop": {"type": "string"}, "total": {"type": "number"}},
    "required": ["shop", "total"],
    "additionalProperties": False,
}
PDF = File("r.pdf", "r", ".pdf", "application/pdf", b"%PDF-1.7", 8)


def _response(**fields: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "stop_reason": "end_turn",
        "model": "claude-opus-5-5",
        "content": [
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text='{"shop": "Ace", "total": 12.5}'),
        ],
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _ask(result: Any, **how: Any) -> tuple[Any, MagicMock]:
    """Ask, with a client that answers ``result`` (or raises it)."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    for create in (client.messages.create, client.beta.messages.create):
        if isinstance(result, Exception):
            create.side_effect = result
        else:
            create.return_value = result
    client.messages.create = AsyncMock(
        side_effect=client.messages.create.side_effect,
        return_value=client.messages.create.return_value,
    )
    client.beta.messages.create = AsyncMock(
        side_effect=client.beta.messages.create.side_effect,
        return_value=client.beta.messages.create.return_value,
    )
    key = how.pop("api_key", "sk-ant-test")
    with patch(
        "spanreed.apis.claude.anthropic.AsyncAnthropic", return_value=client
    ) as made:
        try:
            answer: Any = asyncio.run(
                ClaudeApi(key).ask("What was bought?", SCHEMA, **how)
            )
        except ClaudeError as e:
            answer = e
    client.made = made
    return answer, client


def _status_error(cls: Any, status: int, message: str) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    body = {"type": "error", "error": {"type": "x", "message": message}}
    return cls(message, response=response, body=body)  # type: ignore[no-any-return]


def test_the_answer_has_the_shape_of_the_schema() -> None:
    answer, client = _ask(_response(), instructions="Be brief.")

    assert answer == Answer(
        data={"shop": "Ace", "total": 12.5}, model="claude-opus-5-5"
    )
    client.made.assert_called_once_with(api_key="sk-ant-test")
    sent = client.beta.messages.create.await_args.kwargs
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"] == "Be brief."
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA}
    }
    assert sent["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "What was bought?"}]}
    ]
    # A request that the model declines is handed on in the same call.
    assert sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["fallbacks"] == "default"
    # Thinking and sampling are left to the model: it takes neither setting.
    assert not {"thinking", "temperature", "system_prompt"} & set(sent)
    client.messages.create.assert_not_awaited()


def test_a_model_without_a_fallback_is_asked_plainly() -> None:
    answer, client = _ask(_response(model="claude-haiku-5-5"), model="claude-haiku-5-5")

    assert answer.model == "claude-haiku-5-5"
    sent = client.messages.create.await_args.kwargs
    assert "betas" not in sent and "fallbacks" not in sent and "system" not in sent
    client.beta.messages.create.assert_not_awaited()


def test_files_go_before_the_question() -> None:
    picture = File("p.png", "p", ".png", "image/png; x=1", b"\x89PNG", 5)
    note = File("n.md", "n", ".md", "text/markdown", "# Hi\n".encode(), 5)

    _, client = _ask(_response(), files=[PDF, picture, note])

    content = client.beta.messages.create.await_args.kwargs["messages"][0]["content"]
    assert [part["type"] for part in content] == ["document", "image", "text", "text"]
    assert content[0]["source"] == {
        "type": "base64",
        "media_type": "application/pdf",
        "data": "JVBERi0xLjc=",
    }
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[2]["text"].startswith("The file “n.md”:\n\n# Hi")
    assert content[3]["text"] == "What was bought?"


def test_a_file_that_claude_cannot_read_is_refused() -> None:
    with pytest.raises(ValueError, match="Claude cannot read the file “a.zip”"):
        file_block(File("a.zip", "a", ".zip", "application/zip", b"PK", 2))
    with pytest.raises(ValueError, match="The file “a.txt” is no text."):
        file_block(File("a.txt", "a", ".txt", "text/plain", b"\xff\xfe", 2))


@pytest.mark.parametrize(
    ("result", "said"),
    [
        (
            _status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"),
            "Claude did not accept the API key. Check it in the settings of the "
            "Claude plugin.",
        ),
        (
            _status_error(anthropic.RateLimitError, 429, "slow down"),
            "Claude is asked too much with this API key right now, or the "
            "account is out of credit.",
        ),
        (
            _status_error(anthropic.BadRequestError, 400, "schema: bad keyword"),
            "Claude did not take the request: schema: bad keyword",
        ),
        (
            _status_error(anthropic.InternalServerError, 529, "overloaded"),
            "Claude answered with an error (529): overloaded",
        ),
        (
            anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            "Claude could not be reached.",
        ),
        (
            _response(
                stop_reason="refusal",
                stop_details=SimpleNamespace(explanation="It is about weapons."),
                content=[],
            ),
            "Claude declined to answer. It is about weapons.",
        ),
        (
            _response(stop_reason="max_tokens"),
            "The answer was too long, and was cut off. Ask for less at once.",
        ),
        (_response(content=[]), "Claude gave no answer."),
    ],
)
def test_no_answer_says_why_in_words_for_the_user(result: Any, said: str) -> None:
    answer, _ = _ask(result)

    assert isinstance(answer, ClaudeError) and str(answer) == said
    # Never the key.
    assert "sk-ant" not in str(answer)


def test_without_a_key_claude_is_not_asked() -> None:
    answer, client = _ask(_response(), api_key="")

    assert str(answer) == (
        "Claude has no API key. Set one in the settings of the Claude plugin."
    )
    client.made.assert_not_called()


def test_a_schema_is_an_object_or_the_text_of_one() -> None:
    assert checked_schema(SCHEMA) == SCHEMA
    assert checked_schema(json.dumps(SCHEMA)) == SCHEMA
    for wrong, said in (
        ("{not json", "The schema is not JSON"),
        ([1], "The schema has to be a JSON object."),
        ({"type": "string"}, "The schema has to describe an object"),
        ({"type": "object"}, "The schema has to describe an object"),
    ):
        with pytest.raises(ValueError, match=said):
            checked_schema(wrong)


# -- the plugin and the block ----------------------------------------------------


def test_the_plugin_holds_a_key_and_publishes_the_block() -> None:
    Plugin.reset_registry()
    plugin = ClaudePlugin()

    assert (plugin.name(), plugin.canonical_name()) == ("Claude", "claude")
    assert plugin.has_user_config() is True
    assert [block.id for block in plugin.blocks()] == ["claude.ask"]
    registry = BlockRegistry()
    for block in claude_blocks():
        registry.register(block, fake_plugin())


def _context() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="r1", manual=False),
    )


def test_ask_claude_gives_the_answer_to_the_steps_after_it() -> None:
    api = MagicMock()
    api.ask = AsyncMock(return_value=Answer({"shop": "Ace", "total": 12.5}, "m"))
    inputs = AskClaudeInput(
        prompt="What was bought?",
        schema=json.dumps(SCHEMA),
        instructions="Be brief.",
        files=PDF,
        model="claude-haiku-5-5",
    )

    with patch(
        "spanreed.apis.claude_blocks.ClaudeApi.for_user", AsyncMock(return_value=api)
    ) as for_user:
        output = asyncio.run(AskClaude().run(_context(), inputs))

    assert for_user.await_args_list[0].args[0].id == 4
    api.ask.assert_awaited_once_with(
        "What was bought?",
        SCHEMA,
        model="claude-haiku-5-5",
        instructions="Be brief.",
        files=[PDF],
    )
    assert output == AskClaudeOutput(
        data={"shop": "Ace", "total": 12.5},
        text='{\n  "shop": "Ace",\n  "total": 12.5\n}',
        model="m",
    )


def test_what_cannot_be_asked_fails_before_claude_is_asked() -> None:
    for inputs, said in (
        (AskClaudeInput(prompt="x", schema="{"), "The schema is not JSON"),
        (
            AskClaudeInput(prompt="x", schema=SCHEMA, files=["a path"]),
            "“Files” takes the files of an earlier step",
        ),
    ):
        with patch(
            "spanreed.apis.claude_blocks.ClaudeApi.for_user", AsyncMock()
        ) as for_user, pytest.raises(ValueError, match=said):
            asyncio.run(AskClaude().run(_context(), inputs))
        for_user.assert_not_awaited()


def test_the_editor_stores_the_schema_as_the_object_it_is() -> None:
    edit = editing.fields(AskClaudeInput, {"prompt": "x", "schema": SCHEMA})

    assert json.loads(edit["schema"]["text"]) == SCHEMA
    assert edit["model"]["choices"] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]
    found: list[str] = []
    stored = editing._inputs(
        AskClaudeInput,
        {"prompt": "x", "schema": edit["schema"]["text"], "files": "{{ get.files }}"},
        "Step 1",
        found,
    )
    assert found == []
    assert stored == {"prompt": "x", "schema": SCHEMA, "files": "{{ get.files }}"}
