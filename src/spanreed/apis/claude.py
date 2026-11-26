"""Claude: the plugin that holds a Claude API key, and asks Claude.

The key is the user's own, from the Claude Console. ``ask`` sends one
prompt, with files if there are any, and returns an answer in the shape of
a JSON schema: the API guarantees the shape, so a workflow can rely on it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anthropic

from spanreed.interaction import UserInteractionApi
from spanreed.plugin import Plugin
from spanreed.user import User

if TYPE_CHECKING:
    from spanreed.workflows.blocks import Block
    from spanreed.workflows.files import File

# The models a step can choose from, the first by default.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
# The models that hand a request they decline to another model themselves.
_WITH_FALLBACK = ("claude-opus-5-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Room for an answer. A request without streaming has to end before the
# connection times out, which this does.
MAX_TOKENS = 16000
_IMAGES = ("image/jpeg", "image/png", "image/gif", "image/webp")


@dataclass
class UserConfig:
    api_key: str


class ClaudePlugin(Plugin[UserConfig]):
    """The connection to Claude: the user's API key, and the block that asks."""

    @classmethod
    def name(cls) -> str:
        return "Claude"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def get_config(cls, user: User) -> UserConfig:
        saved = await cls.get_saved_config_data(user) or {}
        return UserConfig(api_key=str(saved.get("api_key") or ""))

    def blocks(self) -> list["Block"]:
        """Return the Claude blocks for workflows."""
        # Imported here: the blocks import this module.
        from spanreed.apis.claude_blocks import claude_blocks

        return claude_blocks()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        api_key = await bot.request_user_input(
            "Please enter your Claude API key.\n"
            "You can create one in the Claude Console, under API keys: "
            "https://platform.claude.com/settings/keys"
        )
        await cls.set_config(user, UserConfig(api_key=api_key.strip()))


@dataclass
class Answer:
    """What Claude answered."""

    # The answer, in the shape of the schema.
    data: Any
    # The model that answered: another than the one asked, if that one
    # declined and handed the request on.
    model: str


class ClaudeError(RuntimeError):
    """Claude gave no answer. The message is for the user."""


def checked_schema(schema: Any) -> dict[str, Any]:
    """Return a JSON schema as the API takes it, from what a step holds.

    A step holds it as an object, or as the text of one. Raise
    ``ValueError`` for anything that is no schema of an object.
    """
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError as e:
            raise ValueError(f"The schema is not JSON: {e}.") from None
    if not isinstance(schema, dict):
        raise ValueError("The schema has to be a JSON object.")
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ValueError(
            "The schema has to describe an object: “type” is “object”, and "
            "“properties” names what the answer holds."
        )
    return schema


def file_block(file: "File") -> dict[str, Any]:
    """Return a file as the part of a prompt that it is.

    Raise ``ValueError`` for a file that Claude cannot read.
    """
    kind = (file.type or "").split(";")[0].strip().lower()
    if kind == "application/pdf" or kind in _IMAGES:
        data = base64.standard_b64encode(file.data).decode("ascii")
        source = {"type": "base64", "media_type": kind, "data": data}
        if kind == "application/pdf":
            return {"type": "document", "source": source, "title": file.name}
        return {"type": "image", "source": source}
    if kind.startswith("text/") or kind in ("application/json", "application/xml"):
        try:
            text = file.data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"The file “{file.name}” is no text.") from None
        return {"type": "text", "text": f"The file “{file.name}”:\n\n{text}"}
    raise ValueError(
        f"Claude cannot read the file “{file.name}” ({kind or 'no type'}). It "
        "reads PDFs, pictures (JPEG, PNG, GIF, WebP) and text."
    )


class ClaudeApi:
    """Asks Claude, with the API key of one user."""

    def __init__(self, api_key: str) -> None:
        """Create the API for a key."""
        self._api_key = api_key

    @classmethod
    async def for_user(cls, user: User) -> "ClaudeApi":
        """Return the API with the key the user gave the plugin."""
        config = await ClaudePlugin.get_config(user)
        return cls(config.api_key)

    async def ask(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        model: str = MODELS[0],
        instructions: str = "",
        files: list["File"] | None = None,
    ) -> Answer:
        """Send one prompt, and return the answer in the shape of the schema.

        Raise ``ClaudeError`` if there is no answer: the key is wrong, the
        request is refused, or Claude declines.
        """
        if not self._api_key:
            raise ClaudeError(
                "Claude has no API key. Set one in the settings of the Claude plugin."
            )
        # The files first: Claude reads what it is asked about before the question.
        content: list[dict[str, Any]] = [file_block(file) for file in files or []]
        content.append({"type": "text", "text": prompt})
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if instructions.strip():
            request["system"] = instructions
        if model in _WITH_FALLBACK:
            # A request that the model declines for its safeguards is handed
            # to another model in the same call.
            request["betas"] = [_FALLBACK_BETA]
            request["fallbacks"] = "default"
        try:
            async with anthropic.AsyncAnthropic(api_key=self._api_key) as client:
                if "betas" in request:
                    response: Any = await client.beta.messages.create(**request)
                else:
                    response = await client.messages.create(**request)
        except anthropic.AuthenticationError:
            raise ClaudeError(
                "Claude did not accept the API key. Check it in the settings "
                "of the Claude plugin."
            ) from None
        except anthropic.PermissionDeniedError as e:
            raise ClaudeError(f"The API key may not do this: {_said(e)}") from None
        except anthropic.RateLimitError:
            raise ClaudeError(
                "Claude is asked too much with this API key right now, or the "
                "account is out of credit."
            ) from None
        except anthropic.BadRequestError as e:
            # Mostly a schema that the API does not take: it says which part.
            raise ClaudeError(f"Claude did not take the request: {_said(e)}") from None
        except anthropic.APIStatusError as e:
            raise ClaudeError(
                f"Claude answered with an error ({e.status_code}): {_said(e)}"
            ) from None
        except anthropic.APIConnectionError:
            raise ClaudeError("Claude could not be reached.") from None

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            why = getattr(details, "explanation", None) or "It gave no reason."
            raise ClaudeError(f"Claude declined to answer. {why}")
        if response.stop_reason == "max_tokens":
            raise ClaudeError(
                "The answer was too long, and was cut off. Ask for less at once."
            )
        text = next(
            (block.text for block in response.content if block.type == "text"), None
        )
        if text is None:
            raise ClaudeError("Claude gave no answer.")
        try:
            data = json.loads(text)
        except ValueError:
            raise ClaudeError(
                "Claude’s answer was not in the shape of the schema."
            ) from None
        return Answer(data=data, model=str(response.model))


def _said(error: anthropic.APIStatusError) -> str:
    """Return what the API said about a request it did not serve."""
    body = error.body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = body["error"].get("message")
        if isinstance(message, str) and message:
            return message
    return str(error.message)
