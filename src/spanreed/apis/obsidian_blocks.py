"""The Obsidian blocks for workflows.

They reach the vault through ``ObsidianApi``, so through the Spanreed plugin
for Obsidian. A block fails while Obsidian does not answer, and the engine
tries the run again.
"""

from __future__ import annotations

import datetime
import mimetypes
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from spanreed.apis.obsidian import ObsidianApi, ObsidianPluginTooOld
from spanreed.apis.obsidian_watch import AnsweringAgain, StoppedAnswering
from spanreed.interaction import UserInteractionApi
from spanreed.workflows.blocks import Action, Block, StepContext
from spanreed.workflows.editing import typed_rows
from spanreed.workflows.values import in_words
from spanreed.workflows.files import File, as_bytes, file_of
from spanreed.config_form import HELP_KEY, described


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

    path: str = described(
        "The path of the note in the vault, such as Daily/2026-05-09.md: "
        "where the Daily notes settings of Obsidian put it. Empty if the "
        "Spanreed plugin for Obsidian is too old to say",
    )
    created: bool = described(
        "Whether the note was made now. No if it was there already",
    )


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

    path: str = described(
        "The path of the note in the vault, such as Daily/2026-05-09.md: "
        "where it is, or where the Daily notes settings of Obsidian would "
        "put it",
    )
    exists: bool = described("Whether the note is there")


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


# The types a property can be given, as Obsidian has them. "any" leaves
# the value as it comes.
PROPERTY_TYPES = ("any", "text", "number", "checkbox", "date", "list")
_YES = ("yes", "true", "on")
_NO = ("no", "false", "off")


def typed_property(name: str, value: Any, kind: str) -> Any:
    """Return the value of a property as its type has it.

    Raise ``ValueError``, with the name of the property, for a value that
    is not of the type and cannot be read as one. Nothing stays nothing:
    such a property is not written.
    """
    if value is None or kind in ("any", ""):
        return value
    wrong = ValueError(
        f"The property “{name}” is a {kind}, and {in_words(value)} is none."
    )
    if kind == "text":
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (str, int, float)):
            return str(value)
        raise wrong
    if kind == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            for number in (int, float):
                try:
                    return number(value.strip())
                except ValueError:
                    continue
        raise wrong
    if kind == "checkbox":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _YES + _NO:
            return value.strip().lower() in _YES
        raise wrong
    if kind == "date":
        if isinstance(value, datetime.datetime):
            return value.date().isoformat()
        if isinstance(value, datetime.date):
            return value.isoformat()
        if isinstance(value, str):
            try:
                # The day of a date with a time too.
                return datetime.date.fromisoformat(value.strip()[:10]).isoformat()
            except ValueError:
                raise wrong from None
        raise wrong
    if kind == "list":
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return [value]
    raise ValueError(f"The property “{name}” has the type “{kind}”, which is none.")


def _typed(properties: dict[str, Any], types: dict[str, str]) -> dict[str, Any]:
    """Return every property as its type has it."""
    return {
        name: typed_property(name, value, types.get(name, "any"))
        for name, value in properties.items()
    }


@dataclass
class SetNotePropertiesInput:
    """Which properties of which note to set."""

    # The path of the note in the vault, such as ``Daily/2026-05-09.md``.
    note: str
    # The value of each property, by its name. A property whose value is
    # empty (``None``) is left alone.
    properties: dict[str, Any] = typed_rows("types", PROPERTY_TYPES)
    # ``changed``: set a property unless the note has this value already.
    # ``empty``: set a property only if the note has no value for it.
    # ``always``: set every property, without reading the note first.
    write_when: Literal["changed", "empty", "always"] = "changed"
    # ``skip and tell me``: go on without writing, and send the user a
    # message that names the note.
    if_note_missing: Literal["skip", "skip and tell me", "fail"] = "skip"
    # The type of a property, by its name: one of ``PROPERTY_TYPES``. A
    # property that is not named is written as its value comes.
    types: dict[str, str] = field(default_factory=dict)


@dataclass
class SetNotePropertiesOutput:
    """What was written."""

    note: str = described("The path of the note")
    written: list[str] = described("The names of the properties that were set")
    count: int = described("How many properties were set")
    summary: str = described(
        "The properties that were set with their values, in one line: " "steps=6227",
    )
    note_missing: bool = described(
        "Whether the note is not there, so that nothing was set",
    )


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
            for name, value in _typed(inputs.properties, inputs.types).items():
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

    path: str = described("The path of the file in the vault")
    written: bool = described("Whether the file was written")
    deleted: bool = described(
        "Whether the file was deleted, because it would have been empty",
    )
    skipped: bool = described(
        "Whether the file was there already and was left as it is",
        default=False,
    )


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
    properties: dict[str, Any] = typed_rows(
        "types", PROPERTY_TYPES, default_factory=dict
    )
    # The text under the properties.
    body: str = ""
    if_exists: Literal["overwrite", "fail"] = "overwrite"
    # The type of a property, by its name, as for "Set note properties".
    types: dict[str, str] = field(default_factory=dict)


@dataclass
class WriteNoteOutput:
    """What was written."""

    path: str = described("The path of the note in the vault")


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
            note_text(_typed(inputs.properties, inputs.types), inputs.body).encode(
                "utf-8"
            ),
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

    path: str = described("The path of the note in the vault")


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

    value: Any = described(
        "The value of the property. Nothing if the note has no such " "property",
    )
    items: list[Any] = described(
        "The value if it is a list, and an empty list if it is anything "
        "else: a step that needs a list always has one",
    )
    found: bool = described("Whether the note has the property")
    note_missing: bool = described("Whether the note is not there")


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

    note: str = described("The path of the note")


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

    rows: list[Any] = described(
        "For a table: one item for each row, with each column under its "
        "name in small letters. The first column is the note, so {{ "
        "each.item.file.path }} is its path. For a list: the values",
    )
    count: int = described("How many rows there are")


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


# -- the files of the vault -----------------------------------------------------


@dataclass
class VaultFile:
    """A file of the vault, by where it is."""

    path: str = described("The path of the file in the vault")
    name: str = described("The name of the file, with its extension")
    stem: str = described("The name up to its extension")
    extension: str = described("The extension with its dot: .pdf. Empty if none")


def _vault_file(path: str) -> VaultFile:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    stem, extension = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    return VaultFile(path=path, name=name, stem=stem, extension=extension)


@dataclass
class ListFilesInput:
    """Which files of the vault."""

    folder: str = described("The folder to look in, such as Assets/scans")
    pattern: str = described(
        "A regular expression that the name of a file has to match somewhere. "
        "Empty: any name",
        default="",
    )
    extension: str = described(
        "Only files with this extension, such as pdf. Empty: any",
        default="",
    )
    leave_out: Any = described(
        "Paths that are not listed: the ones that are dealt with already. "
        "Empty: none",
        default=None,
    )


@dataclass
class ListFilesOutput:
    """The files that were found, by name."""

    files: list[VaultFile] = described("The files, in the order of their names")
    count: int = described("How many files there are")
    first: str = described("The path of the first file. Empty if there is none")


class ListFiles(Action[ListFilesInput, ListFilesOutput]):
    """List the files in a folder of the vault.

    Only the files of the folder itself, not of the folders inside it.
    """

    id = "obsidian.list-files"
    title = "List files"

    async def run(self, ctx: StepContext, inputs: ListFilesInput) -> ListFilesOutput:
        """Return the files of the folder that the inputs ask for."""
        folder = inputs.folder.strip().strip("/")
        try:
            pattern = re.compile(inputs.pattern) if inputs.pattern else None
        except re.error as e:
            raise ValueError(
                f"The pattern “{inputs.pattern}” is no regular expression: {e}."
            ) from None
        extension = inputs.extension.strip().lstrip(".").lower()
        leave_out = inputs.leave_out or []
        if isinstance(leave_out, str):
            leave_out = [leave_out]
        left_out = {str(path) for path in leave_out}
        obsidian = await ObsidianApi.for_user(ctx.user)
        found = []
        for path in sorted(await obsidian.list_dir(folder)):
            file = _vault_file(path)
            # The plugin lists what is under the folder, however deep.
            if path.rsplit("/", 1)[0] != folder and "/" in path:
                continue
            if extension and file.extension.lower() != f".{extension}":
                continue
            if pattern is not None and pattern.search(file.name) is None:
                continue
            if path in left_out:
                continue
            found.append(file)
        return ListFilesOutput(
            files=found, count=len(found), first=found[0].path if found else ""
        )


@dataclass
class ReadFileInput:
    """Which file of the vault."""

    path: str = described("The path of the file in the vault")


class ReadFile(Action[ReadFileInput, File]):
    """Read a file of the vault, for a step that sends it or asks about it.

    The step itself is the file: "Send file" takes ``{{ step.data }}``, and
    "Ask Claude" takes ``{{ step }}`` as one of its files.
    """

    id = "obsidian.read-file"
    title = "Read file"

    async def run(self, ctx: StepContext, inputs: ReadFileInput) -> File:
        """Return the file, with its name and what is in it."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        data = await obsidian.read_binary_file(inputs.path)
        name = inputs.path.rsplit("/", 1)[-1]
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.lower().endswith(".md"):
            kind = "text/markdown"
        return file_of(name, kind, data)


# How many names "add a number" tries before it gives up.
_NUMBERS_TRIED = 50


@dataclass
class MoveFileInput:
    """Which file, and where to."""

    path: str = described("The path of the file in the vault")
    to: str = described("The path it gets: another name, another folder, or both")
    if_exists: Literal["fail", "skip", "add a number"] = field(
        default="fail",
        metadata={
            HELP_KEY: "What to do if there is a file at the new path. "
            "add a number: “Bill.pdf” becomes “Bill 2.pdf”"
        },
    )


@dataclass
class MoveFileOutput:
    """Where the file is now."""

    path: str = described("The path of the file now: the old one if it was skipped")
    moved: bool = described("Whether the file was moved")


class MoveFile(Action[MoveFileInput, MoveFileOutput]):
    """Move a file of the vault, or give it another name."""

    id = "obsidian.move-file"
    title = "Move file"

    async def run(self, ctx: StepContext, inputs: MoveFileInput) -> MoveFileOutput:
        """Move the file, and return where it is."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        target = inputs.to
        dot = target.rfind(".", target.rfind("/") + 1)
        stem, extension = (target[:dot], target[dot:]) if dot > 0 else (target, "")
        for number in range(1, _NUMBERS_TRIED + 1):
            try:
                await obsidian.move_file(inputs.path, target)
            except FileExistsError:
                if inputs.if_exists == "skip":
                    return MoveFileOutput(path=inputs.path, moved=False)
                if inputs.if_exists == "fail":
                    raise FileExistsError(
                        f"There is a file at {target} already."
                    ) from None
                target = f"{stem} {number + 1}{extension}"
            else:
                return MoveFileOutput(path=target, moved=True)
        raise FileExistsError(
            f"There are files at {inputs.to} and at {_NUMBERS_TRIED} names after it."
        )


@dataclass
class DeleteFileInput:
    """Which file of the vault."""

    path: str = described("The path of the file in the vault")
    if_missing: Literal["skip", "fail"] = "skip"


@dataclass
class DeleteFileOutput:
    """What happened to the file."""

    deleted: bool = described("Whether the file was deleted. No if it was not there")


class DeleteFile(Action[DeleteFileInput, DeleteFileOutput]):
    """Delete a file of the vault for good. It is not moved to the trash."""

    id = "obsidian.delete-file"
    title = "Delete file"

    async def run(self, ctx: StepContext, inputs: DeleteFileInput) -> DeleteFileOutput:
        """Delete the file."""
        obsidian = await ObsidianApi.for_user(ctx.user)
        try:
            await obsidian.delete_file(inputs.path)
        except FileNotFoundError:
            if inputs.if_missing == "fail":
                raise
            return DeleteFileOutput(deleted=False)
        return DeleteFileOutput(deleted=True)


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
        ListFiles(),
        ReadFile(),
        MoveFile(),
        DeleteFile(),
        StoppedAnswering(),
        AnsweringAgain(),
    ]
