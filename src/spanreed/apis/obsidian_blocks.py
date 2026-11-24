"""The Obsidian blocks for workflows.

They reach the vault through ``ObsidianApi``, so through the Spanreed plugin
for Obsidian. A block fails while Obsidian does not answer, and the engine
tries the run again.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from spanreed.apis.obsidian import ObsidianApi
from spanreed.interaction import UserInteractionApi
from spanreed.workflows.blocks import Action, Block, NoInput, NoOutput, StepContext


class CreateDailyNote(Action[NoInput, NoOutput]):
    """Create today's daily note from its template, if it is not there.

    Only today's note can be created: Obsidian makes it from the daily note
    template. A note of another day has to be there already.
    """

    id = "obsidian.create-daily-note"
    title = "Create today’s daily note"

    async def run(self, ctx: StepContext, inputs: NoInput) -> NoOutput:
        """Ask Obsidian for today's daily note."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        await obsidian.safe_generate_today_note()
        return NoOutput()


@dataclass
class SetNotePropertiesInput:
    """Which properties of which note to set."""

    # The path of the note in the vault, such as ``Daily/2026-05-09.md``.
    note: str
    # The value of each property, by its name. A property whose value is
    # empty (``None``) is left alone.
    properties: dict[str, Any]
    # ``changed``: set a property unless the note has this value already.
    # ``empty``: set a property only if the note has no value for it.
    # ``always``: set every property, without reading the note first.
    write_when: Literal["changed", "empty", "always"] = "changed"
    # ``skip and tell me``: go on without writing, and send the user a
    # message that names the note.
    if_note_missing: Literal["skip", "skip and tell me", "fail"] = "skip"


@dataclass
class SetNotePropertiesOutput:
    """What was written."""

    note: str
    # The names of the properties that were set.
    written: list[str]
    count: int
    # The properties that were set with their values, as one line of text.
    summary: str
    note_missing: bool


class SetNoteProperties(Action[SetNotePropertiesInput, SetNotePropertiesOutput]):
    """Set properties in the frontmatter of a note."""

    id = "obsidian.set-note-properties"
    title = "Set note properties"

    def writes(self, inputs: dict[str, Any]) -> list[str]:
        """Name each property of the note that the step sets."""
        properties = inputs.get("properties")
        if not isinstance(properties, dict) or not inputs.get("note"):
            return []
        return [f"the property “{name}” of {inputs['note']}" for name in properties]

    async def run(
        self, ctx: StepContext, inputs: SetNotePropertiesInput
    ) -> SetNotePropertiesOutput:
        """Set each property that ``write_when`` says to set."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        written: dict[str, Any] = {}
        note_missing = False
        try:
            for name, value in inputs.properties.items():
                if value is None:
                    continue
                if inputs.write_when != "always":
                    current = await obsidian.get_property(inputs.note, name)
                    if inputs.write_when == "changed" and current == value:
                        continue
                    if inputs.write_when == "empty" and current is not None:
                        continue
                await obsidian.set_value_of_property(inputs.note, name, value)
                written[name] = value
        except FileNotFoundError:
            if inputs.if_note_missing == "fail":
                raise
            note_missing = True
            if inputs.if_note_missing == "skip and tell me":
                bot = await UserInteractionApi.for_user(ctx.user)
                await bot.send_message(
                    f"There is no note {inputs.note}, so nothing was written to it.",
                    parse_html=False,
                )
        return SetNotePropertiesOutput(
            note=inputs.note,
            written=list(written),
            count=len(written),
            summary=", ".join(f"{name}={value}" for name, value in written.items()),
            note_missing=note_missing,
        )


@dataclass
class WriteFileInput:
    """Which file to write, and what to put in it."""

    # The path of the file in the vault. Missing folders are created.
    path: str
    content: str
    if_exists: Literal["overwrite", "fail"] = "overwrite"
    # ``delete``: with no content, delete the file instead of emptying it.
    if_empty: Literal["write", "delete"] = "write"


@dataclass
class WriteFileOutput:
    """What happened to the file."""

    path: str
    written: bool
    deleted: bool


class WriteFile(Action[WriteFileInput, WriteFileOutput]):
    """Write a text file into the vault."""

    id = "obsidian.write-file"
    title = "Write file"

    def writes(self, inputs: dict[str, Any]) -> list[str]:
        """Name the file that the step writes."""
        return [f"the file {inputs['path']}"] if inputs.get("path") else []

    async def run(self, ctx: StepContext, inputs: WriteFileInput) -> WriteFileOutput:
        """Write the file, or delete it if it would be empty."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        if inputs.content == "" and inputs.if_empty == "delete":
            try:
                await obsidian.delete_file(inputs.path)
            except FileNotFoundError:
                # It was never written, or it is gone already.
                return WriteFileOutput(path=inputs.path, written=False, deleted=False)
            return WriteFileOutput(path=inputs.path, written=False, deleted=True)
        await obsidian.write_binary_file(
            inputs.path,
            inputs.content.encode("utf-8"),
            overwrite=inputs.if_exists == "overwrite",
        )
        return WriteFileOutput(path=inputs.path, written=True, deleted=False)


@dataclass
class WriteNoteInput:
    """Which note to write, with which properties and text."""

    # The path of the note in the vault. Missing folders are created.
    path: str
    # The value of each property, by its name, in the order they are
    # written. A property whose value is empty (``None``) is left out.
    properties: dict[str, Any] = field(default_factory=dict)
    # The text under the properties.
    body: str = ""
    if_exists: Literal["overwrite", "fail"] = "overwrite"


@dataclass
class WriteNoteOutput:
    """What was written."""

    path: str


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _property_value(value: Any) -> Any:
    """Return a value as it goes into the frontmatter.

    A text that is a date, ``2026-05-09``, becomes a date, so that it is
    written without quotes and Obsidian shows the property as a date.
    """
    if isinstance(value, str) and _ISO_DATE_RE.match(value):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return value
    return value


def note_text(properties: dict[str, Any], body: str) -> str:
    """Return the text of a note: its properties as frontmatter, then the body."""
    frontmatter = {
        name: _property_value(value)
        for name, value in properties.items()
        if value is not None
    }
    text = f"{body}\n" if body else ""
    if not frontmatter:
        return text
    block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{block}\n---\n\n{text}"


class WriteNote(Action[WriteNoteInput, WriteNoteOutput]):
    """Write a whole note: properties and text.

    The note replaces one that is already at the path, so writing it again
    after a failure leaves one copy.
    """

    id = "obsidian.write-note"
    title = "Write note"

    def writes(self, inputs: dict[str, Any]) -> list[str]:
        """Name the note that the step writes, as "Write file" names a file."""
        return [f"the file {inputs['path']}"] if inputs.get("path") else []

    async def run(self, ctx: StepContext, inputs: WriteNoteInput) -> WriteNoteOutput:
        """Write the note."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        await obsidian.write_binary_file(
            inputs.path,
            note_text(inputs.properties, inputs.body).encode("utf-8"),
            overwrite=inputs.if_exists == "overwrite",
        )
        return WriteNoteOutput(path=inputs.path)


def obsidian_blocks() -> list[Block]:
    """Return the blocks that the Obsidian plugin publishes."""
    return [CreateDailyNote(), SetNoteProperties(), WriteFile(), WriteNote()]
