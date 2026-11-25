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

from spanreed.apis.obsidian import ObsidianApi, ObsidianPluginTooOld
from spanreed.apis.obsidian_watch import StoppedAnswering
from spanreed.interaction import UserInteractionApi
from spanreed.workflows.blocks import Action, Block, StepContext
from spanreed.workflows.files import as_bytes


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _today(ctx: StepContext) -> datetime.date:
    """Return today's date where the user is."""
    return datetime.datetime.now(ctx.user.tzinfo).date()


@dataclass
class DailyNoteInput:
    """Which day's note."""

    # The day, as ``2026-05-09``: ``{{ trigger.date }}``. Empty for today,
    # where the user is.
    date: str = ""


def _day(ctx: StepContext, inputs: DailyNoteInput) -> datetime.date:
    """Return the day that a step about a daily note is about."""
    text = str(inputs.date or "").strip()
    if not text:
        return _today(ctx)
    try:
        if not _DATE_RE.fullmatch(text):
            raise ValueError(text)
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"“{text}” is not a date. Write it as 2026-05-09, or leave it "
            "empty for today."
        ) from None


def _daily_note_in_words(inputs: dict[str, Any]) -> str:
    """Return which daily note a step is about, from its stored inputs."""
    date = str(inputs.get("date") or "").strip()
    return f"the daily note of {date}" if date else "today’s daily note"


@dataclass
class CreateDailyNoteOutput:
    """Where the note is."""

    # The path of the note in the vault, such as ``Daily/2026-05-09.md``:
    # where the Daily notes settings of Obsidian put it. Empty if the
    # Spanreed plugin for Obsidian is too old to say.
    path: str
    # Whether the note was made now. False if it was there already.
    created: bool


class CreateDailyNote(Action[DailyNoteInput, CreateDailyNoteOutput]):
    """Create the daily note of a day from its template, if it is not there.

    Obsidian makes it where its Daily notes settings say, and ``path`` is
    where that is. The steps after this one write to ``path``, and need
    not know the folder or the date format.
    """

    id = "obsidian.create-daily-note"
    title = "Create daily note"

    def stands_for(self, inputs: dict[str, Any]) -> dict[str, str]:
        """Say which note ``path`` is."""
        return {"path": _daily_note_in_words(inputs)}

    async def run(
        self, ctx: StepContext, inputs: DailyNoteInput
    ) -> CreateDailyNoteOutput:
        """Ask Obsidian for the daily note of the day."""
        day = _day(ctx, inputs)
        obsidian = await ObsidianApi.for_user(ctx.user)
        note = await obsidian.create_daily_note(day)
        if not note.path and day != _today(ctx):
            # A plugin from before it took a day made today's note.
            raise ObsidianPluginTooOld(
                "The Spanreed plugin for Obsidian is too old to create the "
                "daily note of another day than today. Update the plugin."
            )
        return CreateDailyNoteOutput(path=note.path, created=note.created)


@dataclass
class FindDailyNoteOutput:
    """Where the note is, or would be."""

    # The path of the note in the vault, such as ``Daily/2026-05-09.md``:
    # where it is, or where the Daily notes settings of Obsidian would put
    # it.
    path: str
    # Whether the note is there.
    exists: bool


class FindDailyNote(Action[DailyNoteInput, FindDailyNoteOutput]):
    """Say where the daily note of a day is, without creating it.

    ``path`` is where the Daily notes settings of Obsidian put the note of
    that day, also while it is not there. A step that writes to it then
    skips it, fails, or makes it, as that step is set.
    """

    id = "obsidian.find-daily-note"
    title = "Find daily note"

    def stands_for(self, inputs: dict[str, Any]) -> dict[str, str]:
        """Say which note ``path`` is."""
        return {"path": _daily_note_in_words(inputs)}

    async def run(
        self, ctx: StepContext, inputs: DailyNoteInput
    ) -> FindDailyNoteOutput:
        """Ask Obsidian where the daily note of the day is."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        note = await obsidian.find_daily_note(_day(ctx, inputs))
        return FindDailyNoteOutput(path=note.path, exists=note.exists)


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
    # A text, or the data of a file from another step.
    content: Any
    # ``skip``: leave a file that is there as it is, and go on.
    if_exists: Literal["overwrite", "skip", "fail"] = "overwrite"
    # ``delete``: with no content, delete the file instead of emptying it.
    if_empty: Literal["write", "delete"] = "write"


@dataclass
class WriteFileOutput:
    """What happened to the file."""

    path: str
    written: bool
    deleted: bool
    # Whether the file was there already and was left as it is.
    skipped: bool = False


class WriteFile(Action[WriteFileInput, WriteFileOutput]):
    """Write a file into the vault: a text, or a file from another step."""

    id = "obsidian.write-file"
    title = "Write file"

    def writes(self, inputs: dict[str, Any]) -> list[str]:
        """Name the file that the step writes."""
        return [f"the file {inputs['path']}"] if inputs.get("path") else []

    async def run(self, ctx: StepContext, inputs: WriteFileInput) -> WriteFileOutput:
        """Write the file, or delete it if it would be empty."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        data = as_bytes(inputs.content)
        if not data and inputs.if_empty == "delete":
            try:
                await obsidian.delete_file(inputs.path)
            except FileNotFoundError:
                # It was never written, or it is gone already.
                return WriteFileOutput(path=inputs.path, written=False, deleted=False)
            return WriteFileOutput(path=inputs.path, written=False, deleted=True)
        try:
            await obsidian.write_binary_file(
                inputs.path, data, overwrite=inputs.if_exists == "overwrite"
            )
        except FileExistsError:
            if inputs.if_exists != "skip":
                raise
            return WriteFileOutput(
                path=inputs.path, written=False, deleted=False, skipped=True
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


@dataclass
class AppendToNoteInput:
    """What to add to which note."""

    # The path of the note in the vault. A note that is not there is made.
    path: str
    text: str
    # The heading to add the text under, at the end of its section. A
    # heading that is not there is added at the end of the note.
    heading: str | None = None


@dataclass
class AppendToNoteOutput:
    """Where the text went."""

    path: str


class AppendToNote(Action[AppendToNoteInput, AppendToNoteOutput]):
    """Add text to the end of a note, or of a section of it.

    Unlike "Write note", this keeps what the note holds. Doing it again
    after a failure adds the text again.
    """

    id = "obsidian.append-to-note"
    title = "Append to note"

    async def run(
        self, ctx: StepContext, inputs: AppendToNoteInput
    ) -> AppendToNoteOutput:
        """Add the text."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        await obsidian.append_to_note(inputs.path, inputs.text, inputs.heading or None)
        return AppendToNoteOutput(path=inputs.path)


@dataclass
class GetNotePropertyInput:
    """Which property of which note to read."""

    # The path of the note in the vault, such as ``Daily/2026-05-09.md``.
    note: str
    property: str
    # ``empty``: go on as if the note had no such property.
    if_note_missing: Literal["empty", "fail"] = "empty"


@dataclass
class GetNotePropertyOutput:
    """What the note holds."""

    # ``None`` if the note has no such property.
    value: Any
    # The value if it is a list, and an empty list if it is anything else.
    # So a step that needs a list has one, whatever the note holds.
    items: list[Any]
    # Whether the note has the property.
    found: bool
    note_missing: bool


class GetNoteProperty(Action[GetNotePropertyInput, GetNotePropertyOutput]):
    """Read a property from the frontmatter of a note."""

    id = "obsidian.get-note-property"
    title = "Get note property"

    async def run(
        self, ctx: StepContext, inputs: GetNotePropertyInput
    ) -> GetNotePropertyOutput:
        """Return the value of the property."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        note_missing = False
        try:
            value = await obsidian.get_property(inputs.note, inputs.property)
        except FileNotFoundError:
            if inputs.if_note_missing == "fail":
                raise
            value, note_missing = None, True
        return GetNotePropertyOutput(
            value=value,
            items=list(value) if isinstance(value, list) else [],
            found=value is not None,
            note_missing=note_missing,
        )


@dataclass
class AddToListPropertyInput:
    """What to add to which list of which note."""

    note: str
    property: str
    value: Any


@dataclass
class AddToListPropertyOutput:
    """Where the value went."""

    note: str


class AddToListProperty(Action[AddToListPropertyInput, AddToListPropertyOutput]):
    """Add a value to a property of a note that holds a list.

    The property is made if the note does not have it.
    """

    id = "obsidian.add-to-list-property"
    title = "Add to list property"

    def writes(self, inputs: dict[str, Any]) -> list[str]:
        """Name the property, as "Set note properties" names the ones it sets."""
        if not inputs.get("note") or not inputs.get("property"):
            return []
        return [f"the property “{inputs['property']}” of {inputs['note']}"]

    async def run(
        self, ctx: StepContext, inputs: AddToListPropertyInput
    ) -> AddToListPropertyOutput:
        """Add the value."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        await obsidian.add_value_to_list_property(
            inputs.note, inputs.property, inputs.value
        )
        return AddToListPropertyOutput(note=inputs.note)


@dataclass
class QueryDataviewInput:
    """What to ask Dataview."""

    # A ``table`` or a ``list`` query, as in a note.
    query: str


@dataclass
class QueryDataviewOutput:
    """What Dataview found."""

    # For a table: one thing for each row, with a value for each column
    # under the column's name in small letters. The first column of a
    # table is the note: ``{{ each.item.file.path }}`` is its path. For a
    # list: the values.
    rows: list[Any]
    count: int


class QueryDataview(Action[QueryDataviewInput, QueryDataviewOutput]):
    """Run a Dataview query in the vault, and give its rows.

    The vault needs the Dataview plugin.
    """

    id = "obsidian.query-dataview"
    title = "Query Dataview"

    async def run(
        self, ctx: StepContext, inputs: QueryDataviewInput
    ) -> QueryDataviewOutput:
        """Return the rows of the query."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        rows = await obsidian.query_dataview(inputs.query)
        if not isinstance(rows, list):
            raise ValueError("The query gave neither a table nor a list.")
        return QueryDataviewOutput(rows=rows, count=len(rows))


def obsidian_blocks() -> list[Block]:
    """Return the blocks that the Obsidian plugin publishes."""
    return [
        CreateDailyNote(),
        FindDailyNote(),
        QueryDataview(),
        GetNoteProperty(),
        SetNoteProperties(),
        AddToListProperty(),
        WriteFile(),
        WriteNote(),
        AppendToNote(),
        StoppedAnswering(),
    ]
