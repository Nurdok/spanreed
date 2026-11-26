"""The Claude block for workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from spanreed.apis.claude import ClaudeApi, checked_schema
from spanreed.config_form import described
from spanreed.workflows.blocks import Action, Block, StepContext
from spanreed.workflows.files import File


@dataclass
class AskClaudeInput:
    """What to ask, and the shape of the answer."""

    prompt: str = described("What Claude is asked, with the values it is asked about")
    schema: Any = described(
        "A JSON schema of an object: “properties” names what the answer "
        "holds. A step after this one reads them as {{ step.data.name }}"
    )
    instructions: str = described(
        "How Claude is to go about it, whatever it is asked. Empty: nothing more",
        default="",
    )
    files: Any = described(
        "A file, or a list of files, of an earlier step: PDFs, pictures and "
        "text. Empty: none",
        default=None,
    )
    model: Literal["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"] = field(
        default="claude-opus-5-5"
    )


@dataclass
class AskClaudeOutput:
    """What Claude answered."""

    data: Any = described(
        "The answer, in the shape of the schema: {{ step.data.name }} is "
        "the property “name”"
    )
    text: str = described("The answer as JSON text, for a file or a message")
    model: str = described(
        "The model that answered. Another than the one asked, if that one "
        "declined and handed the request on"
    )


def _files(given: Any) -> list[File]:
    """Return the files a step was given: one, a list of them, or none."""
    if given is None or given == "" or given == []:
        return []
    files = given if isinstance(given, (list, tuple)) else [given]
    for file in files:
        if not isinstance(file, File):
            raise ValueError(
                "“Files” takes the files of an earlier step, such as the "
                "attachments of an email."
            )
    return list(files)


class AskClaude(Action[AskClaudeInput, AskClaudeOutput]):
    """Ask Claude, and get an answer in a shape that the steps after can use.

    The answer has the properties that the schema names, each of the type
    the schema gives it: the API guarantees the shape. What is sent goes
    to Anthropic, with the user's own API key.
    """

    id = "claude.ask"
    title = "Ask Claude"

    async def run(self, ctx: StepContext, inputs: AskClaudeInput) -> AskClaudeOutput:
        """Send the prompt and return the answer."""
        schema = checked_schema(inputs.schema)
        files = _files(inputs.files)
        claude = await ClaudeApi.for_user(ctx.user)
        answer = await claude.ask(
            inputs.prompt,
            schema,
            model=inputs.model,
            instructions=inputs.instructions,
            files=files,
        )
        return AskClaudeOutput(
            data=answer.data,
            text=json.dumps(answer.data, ensure_ascii=False, indent=2),
            model=answer.model,
        )


def claude_blocks() -> list[Block]:
    """Return the blocks that the Claude plugin publishes."""
    return [AskClaude()]
