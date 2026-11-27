import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.obsidian import DailyNote, ObsidianPluginTooOld
from spanreed.apis.obsidian_blocks import (
    DeleteFile,
    DeleteFileInput,
    ListFiles,
    ListFilesInput,
    ListFilesOutput,
    MoveFile,
    MoveFileInput,
    MoveFileOutput,
    ReadFile,
    ReadFileInput,
    AddToListProperty,
    AddToListPropertyInput,
    AppendToNote,
    AppendToNoteInput,
    GetNoteProperty,
    GetNotePropertyInput,
    QueryDataview,
    QueryDataviewInput,
    CreateDailyNote,
    CreateDailyNoteOutput,
    DailyNoteInput,
    FindDailyNote,
    FindDailyNoteOutput,
    SetNoteProperties,
    SetNotePropertiesInput,
    SetNotePropertiesOutput,
    WriteFile,
    WriteFileInput,
    WriteFileOutput,
    WriteNote,
    WriteNoteInput,
    WriteNoteOutput,
    obsidian_blocks,
    typed_property,
)
from spanreed.test_utils import (
    mock_user_find_by_id,
    patch_obsidian,
    patch_user_interaction,
)
from spanreed.workflows.blocks import BlockRegistry, RunInfo, StepContext
from spanreed.workflows.testing import fake_plugin

NOTE = "Daily/2026-05-09.md"


def _context() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="r1", manual=False),
    )


def _set(mock_obsidian: AsyncMock, **inputs: Any) -> SetNotePropertiesOutput:
    values: dict[str, Any] = {"note": NOTE, "properties": {"steps": 6227}}
    values.update(inputs)
    return asyncio.run(
        SetNoteProperties().run(_context(), SetNotePropertiesInput(**values))
    )


def _sets(mock_obsidian: AsyncMock) -> list[tuple[Any, ...]]:
    return [c.args for c in mock_obsidian.set_value_of_property.await_args_list]


def test_the_obsidian_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in obsidian_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == [
        "obsidian.create-daily-note",
        "obsidian.find-daily-note",
        "obsidian.query-dataview",
        "obsidian.get-note-property",
        "obsidian.set-note-properties",
        "obsidian.add-to-list-property",
        "obsidian.write-file",
        "obsidian.write-note",
        "obsidian.append-to-note",
        "obsidian.list-files",
        "obsidian.read-file",
        "obsidian.move-file",
        "obsidian.delete-file",
        "obsidian.stopped-answering",
        "obsidian.answering-again",
    ]


TODAY = datetime.date(2026, 5, 9)


def _create(date: str = "") -> CreateDailyNoteOutput:
    with patch("spanreed.apis.obsidian_blocks._today", return_value=TODAY):
        return asyncio.run(CreateDailyNote().run(_context(), DailyNoteInput(date)))


def _find(date: str = "") -> FindDailyNoteOutput:
    with patch("spanreed.apis.obsidian_blocks._today", return_value=TODAY):
        return asyncio.run(FindDailyNote().run(_context(), DailyNoteInput(date)))


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_create_daily_note_makes_todays_note_and_says_where_it_is(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.create_daily_note = AsyncMock(
        return_value=DailyNote("Journal/09.05.2026.md", exists=True, created=True)
    )

    output = _create()

    # Today where the user is, so that Obsidian's clock does not decide.
    mock_obsidian.create_daily_note.assert_awaited_once_with(TODAY)
    assert output == CreateDailyNoteOutput(path="Journal/09.05.2026.md", created=True)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_create_daily_note_makes_the_note_of_the_day_it_is_given(
    mock_obsidian: AsyncMock,
) -> None:
    output = _create(" 2026-03-01 ")

    mock_obsidian.create_daily_note.assert_awaited_once_with(datetime.date(2026, 3, 1))
    assert output == CreateDailyNoteOutput(path="Daily/2026-03-01.md", created=False)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_what_is_not_a_date_is_refused_before_obsidian_is_asked(
    mock_obsidian: AsyncMock,
) -> None:
    for date in ("tomorrow", "2026-13-45", "20260301", "2026-W10-1", "01/03/2026"):
        for step in (_create, _find):
            with pytest.raises(ValueError) as error:
                step(date)
            assert str(error.value) == (
                f"“{date}” is not a date. Write it as 2026-05-09, or leave it "
                "empty for today."
            )

    mock_obsidian.create_daily_note.assert_not_awaited()
    mock_obsidian.find_daily_note.assert_not_awaited()


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_an_older_plugin_still_makes_todays_note_but_cannot_say_where(
    mock_obsidian: AsyncMock,
) -> None:
    # What a workflow from before the path was there does: it goes on.
    mock_obsidian.create_daily_note = AsyncMock(
        return_value=DailyNote(path="", exists=True)
    )

    assert _create() == CreateDailyNoteOutput(path="", created=False)
    assert _create("2026-05-09") == CreateDailyNoteOutput(path="", created=False)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_an_older_plugin_cannot_make_the_note_of_another_day(
    mock_obsidian: AsyncMock,
) -> None:
    # It made today's note, whatever the day. That is not what was asked.
    mock_obsidian.create_daily_note = AsyncMock(
        return_value=DailyNote(path="", exists=True)
    )

    with pytest.raises(ObsidianPluginTooOld, match="Update the plugin."):
        _create("2026-05-08")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_find_daily_note_says_where_the_note_of_a_day_is(
    mock_obsidian: AsyncMock,
) -> None:
    for exists in (True, False):
        mock_obsidian.find_daily_note = AsyncMock(
            return_value=DailyNote("Daily/2026-03-01.md", exists=exists)
        )

        output = _find("2026-03-01")

        mock_obsidian.find_daily_note.assert_awaited_once_with(
            datetime.date(2026, 3, 1)
        )
        assert output == FindDailyNoteOutput(path="Daily/2026-03-01.md", exists=exists)
    # Nothing is made, also where the note is not there.
    mock_obsidian.create_daily_note.assert_not_awaited()


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_find_daily_note_is_about_today_when_no_day_is_given(
    mock_obsidian: AsyncMock,
) -> None:
    assert _find() == FindDailyNoteOutput(path="Daily/2026-05-09.md", exists=True)

    mock_obsidian.find_daily_note.assert_awaited_once_with(TODAY)


def test_a_step_about_a_daily_note_says_which_note_its_path_is() -> None:
    for block in (CreateDailyNote(), FindDailyNote()):
        assert block.stands_for({}) == {"path": "today’s daily note"}
        assert block.stands_for({"date": ""}) == {"path": "today’s daily note"}
        assert block.stands_for({"date": "{{ trigger.date }}"}) == {
            "path": "the daily note of {{ trigger.date }}"
        }


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_a_property_is_set_when_the_note_has_another_value(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.get_property.return_value = 5000

    output = _set(mock_obsidian)

    assert _sets(mock_obsidian) == [(NOTE, "steps", 6227)]
    assert output == SetNotePropertiesOutput(
        note=NOTE,
        written=["steps"],
        count=1,
        summary="steps=6227",
        note_missing=False,
    )


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_a_property_that_has_the_value_already_is_left_alone(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.get_property.return_value = 6227

    output = _set(mock_obsidian)

    assert _sets(mock_obsidian) == []
    assert (output.written, output.count, output.summary) == ([], 0, "")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_only_the_properties_that_changed_and_have_a_value_are_set(
    mock_obsidian: AsyncMock,
) -> None:
    in_note = {"resting-heart-rate": 58, "heart-rate-avg": 70}
    mock_obsidian.get_property.side_effect = lambda note, name: in_note.get(name)
    properties = {
        "resting-heart-rate": 58,  # the same
        "heart-rate-avg": 71,  # changed
        "heart-rate-p95": 120,  # new
        "vo2-max": None,  # the day has no value for it
    }

    output = _set(mock_obsidian, properties=properties)

    assert _sets(mock_obsidian) == [
        (NOTE, "heart-rate-avg", 71),
        (NOTE, "heart-rate-p95", 120),
    ]
    assert output.written == ["heart-rate-avg", "heart-rate-p95"]
    assert output.summary == "heart-rate-avg=71, heart-rate-p95=120"
    # The property without a value was not even read.
    assert all(
        c.args[1] != "vo2-max" for c in mock_obsidian.get_property.await_args_list
    )


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_empty_writes_only_a_property_the_note_does_not_have(
    mock_obsidian: AsyncMock,
) -> None:
    in_note = {"weight": 80.5}
    mock_obsidian.get_property.side_effect = lambda note, name: in_note.get(name)

    output = _set(
        mock_obsidian,
        properties={"weight": 79.9, "fat-mass": 12.1},
        write_when="empty",
    )

    # A value that is there, perhaps corrected by hand, is never replaced.
    assert _sets(mock_obsidian) == [(NOTE, "fat-mass", 12.1)]
    assert output.written == ["fat-mass"]


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_always_sets_every_property_without_reading_the_note(
    mock_obsidian: AsyncMock,
) -> None:
    output = _set(mock_obsidian, write_when="always")

    mock_obsidian.get_property.assert_not_awaited()
    assert _sets(mock_obsidian) == [(NOTE, "steps", 6227)]
    assert output.count == 1


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_a_missing_note_is_skipped(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.get_property.side_effect = FileNotFoundError("file not found")

    output = _set(mock_obsidian)

    assert _sets(mock_obsidian) == []
    assert output.note_missing
    assert output.count == 0


@patch_obsidian("spanreed.apis.obsidian_blocks")
@patch_user_interaction("spanreed.apis.obsidian_blocks")
def test_a_missing_note_is_skipped_without_a_word(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    mock_obsidian.get_property.side_effect = FileNotFoundError("file not found")

    _set(mock_obsidian)

    mock_bot.send_message.assert_not_called()


@patch_obsidian("spanreed.apis.obsidian_blocks")
@patch_user_interaction("spanreed.apis.obsidian_blocks")
def test_a_missing_note_can_be_skipped_with_a_message(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    mock_obsidian.get_property.side_effect = FileNotFoundError("file not found")

    output = _set(mock_obsidian, if_note_missing="skip and tell me")

    assert output.note_missing
    assert output.count == 0
    # Plain text: the path of a note can hold anything.
    mock_bot.send_message.assert_awaited_once_with(
        "There is no note Daily/2026-05-09.md, so nothing was written to it.",
        parse_html=False,
    )


@patch_obsidian("spanreed.apis.obsidian_blocks")
@patch_user_interaction("spanreed.apis.obsidian_blocks")
def test_a_note_that_is_there_gives_no_message(
    mock_bot: AsyncMock, mock_obsidian: AsyncMock
) -> None:
    mock_obsidian.get_property.return_value = None

    output = _set(mock_obsidian, if_note_missing="skip and tell me")

    assert output.count == 1
    mock_bot.send_message.assert_not_called()


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_a_missing_note_can_fail_the_step(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.set_value_of_property.side_effect = FileNotFoundError("gone")

    with pytest.raises(FileNotFoundError):
        _set(mock_obsidian, write_when="always", if_note_missing="fail")


def _write(mock_obsidian: AsyncMock, **inputs: Any) -> WriteFileOutput:
    values: dict[str, Any] = {"path": "Health/raw/steps/2026-05-09.jsonl"}
    values.update(inputs)
    return asyncio.run(WriteFile().run(_context(), WriteFileInput(**values)))


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_writes_the_text_over_the_file(mock_obsidian: AsyncMock) -> None:
    output = _write(mock_obsidian, content='{"count": 5}\n')

    mock_obsidian.write_binary_file.assert_awaited_once_with(
        "Health/raw/steps/2026-05-09.jsonl", b'{"count": 5}\n', overwrite=True
    )
    assert (output.written, output.deleted) == (True, False)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_can_refuse_to_replace_a_file(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.write_binary_file.side_effect = FileExistsError("exists")

    with pytest.raises(FileExistsError):
        _write(mock_obsidian, content="text", if_exists="fail")

    assert mock_obsidian.write_binary_file.await_args.kwargs == {"overwrite": False}


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_writes_an_empty_file_unless_told_to_delete(
    mock_obsidian: AsyncMock,
) -> None:
    output = _write(mock_obsidian, content="")

    mock_obsidian.write_binary_file.assert_awaited_once()
    mock_obsidian.delete_file.assert_not_awaited()
    assert output.written


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_deletes_a_file_that_would_be_empty(
    mock_obsidian: AsyncMock,
) -> None:
    output = _write(mock_obsidian, content="", if_empty="delete")

    mock_obsidian.delete_file.assert_awaited_once_with(
        "Health/raw/steps/2026-05-09.jsonl"
    )
    mock_obsidian.write_binary_file.assert_not_awaited()
    assert (output.written, output.deleted) == (False, True)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_deleting_a_file_that_is_not_there_is_fine(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.delete_file.side_effect = FileNotFoundError("file not found")

    output = _write(mock_obsidian, content="", if_empty="delete")

    assert (output.written, output.deleted) == (False, False)


def _note(mock_obsidian: AsyncMock, **inputs: Any) -> str:
    """Write a note with the block, and return the text that was written."""
    values: dict[str, Any] = {"path": "Fitness/Sets/2026-05-09/Squat set 1.md"}
    values.update(inputs)
    output = asyncio.run(WriteNote().run(_context(), WriteNoteInput(**values)))
    assert output == WriteNoteOutput(path=values["path"])
    (call,) = mock_obsidian.write_binary_file.await_args_list
    assert call.args[0] == values["path"]
    text: str = call.args[1].decode("utf-8")
    return text


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_writes_the_properties_in_order_and_then_the_body(
    mock_obsidian: AsyncMock,
) -> None:
    text = _note(
        mock_obsidian,
        properties={
            "type": "workout-set",
            "exercise": "Squat",
            "weight_kg": 100.0,
            "reps": 5,
            "tags": ["workout-set"],
        },
        body="Squat — set 1: 100 kg × 5",
    )

    assert text == (
        "---\n"
        "type: workout-set\n"
        "exercise: Squat\n"
        "weight_kg: 100.0\n"
        "reps: 5\n"
        "tags:\n"
        "- workout-set\n"
        "---\n"
        "\n"
        "Squat — set 1: 100 kg × 5\n"
    )
    assert mock_obsidian.write_binary_file.await_args.kwargs == {"overwrite": True}


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_leaves_out_a_property_with_no_value(
    mock_obsidian: AsyncMock,
) -> None:
    text = _note(mock_obsidian, properties={"reps": 5, "rpe": None, "weight_kg": 0})

    # Nothing is not the same as zero.
    assert text == "---\nreps: 5\nweight_kg: 0\n---\n\n"


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_writes_a_date_as_a_date(mock_obsidian: AsyncMock) -> None:
    text = _note(
        mock_obsidian,
        properties={
            "date": "2026-05-09",
            "title": "2026-05-09 legs",
            "bad": "2026-13-40",
        },
    )

    # Without quotes, so that Obsidian shows the property as a date. A text
    # that only looks like a date stays a text.
    assert text == (
        "---\ndate: 2026-05-09\ntitle: 2026-05-09 legs\nbad: '2026-13-40'\n---\n\n"
    )


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_keeps_letters_of_every_script(mock_obsidian: AsyncMock) -> None:
    text = _note(mock_obsidian, properties={"exercise": "סקוואט"}, body="—")

    assert text == "---\nexercise: סקוואט\n---\n\n—\n"


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_without_properties_writes_only_the_body(
    mock_obsidian: AsyncMock,
) -> None:
    assert _note(mock_obsidian, body="Just text") == "Just text\n"


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_can_refuse_to_replace_a_note(mock_obsidian: AsyncMock) -> None:
    _note(mock_obsidian, body="x", if_exists="fail")

    assert mock_obsidian.write_binary_file.await_args.kwargs == {"overwrite": False}


def test_the_blocks_name_what_they_write() -> None:
    # From the inputs as they are stored: the templates are not filled in.
    assert SetNoteProperties().writes(
        {
            "note": "Daily/{{ trigger.date }}.md",
            "properties": {"weight": "x", "steps": 1},
        }
    ) == [
        "the property “weight” of Daily/{{ trigger.date }}.md",
        "the property “steps” of Daily/{{ trigger.date }}.md",
    ]
    assert SetNoteProperties().writes({"properties": {"weight": "x"}}) == []
    # A note is a file: the two blocks can write the same one.
    assert WriteFile().writes({"path": "Notes/a.md"}) == ["the file Notes/a.md"]
    assert WriteNote().writes({"path": "Notes/a.md"}) == ["the file Notes/a.md"]
    assert WriteNote().writes({}) == []
    assert CreateDailyNote().writes({}) == []


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_writes_the_data_of_a_file_from_another_step(
    mock_obsidian: AsyncMock,
) -> None:
    output = _write(mock_obsidian, path="Receipts/May.pdf", content=b"%PDF-1")

    mock_obsidian.write_binary_file.assert_awaited_once_with(
        "Receipts/May.pdf", b"%PDF-1", overwrite=True
    )
    assert (output.written, output.skipped) == (True, False)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_can_leave_a_file_that_is_there(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.write_binary_file.side_effect = FileExistsError("exists")

    output = _write(mock_obsidian, content=b"%PDF-1", if_exists="skip")

    assert mock_obsidian.write_binary_file.await_args.kwargs == {"overwrite": False}
    assert (output.written, output.skipped, output.deleted) == (False, True, False)

    with pytest.raises(FileExistsError):
        _write(mock_obsidian, content=b"%PDF-1", if_exists="fail")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_file_deletes_for_empty_data_too(mock_obsidian: AsyncMock) -> None:
    output = _write(mock_obsidian, content=b"", if_empty="delete")

    mock_obsidian.write_binary_file.assert_not_called()
    assert output.deleted


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_append_to_note_adds_text_to_the_end_or_under_a_heading(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.append_to_note = AsyncMock()

    output = asyncio.run(
        AppendToNote().run(
            _context(), AppendToNoteInput(path="Books/Dune.md", text="# Dune")
        )
    )
    asyncio.run(
        AppendToNote().run(
            _context(),
            AppendToNoteInput(path="Daily/today.md", text="A line.", heading="Log"),
        )
    )

    assert output.path == "Books/Dune.md"
    assert [call.args for call in mock_obsidian.append_to_note.await_args_list] == [
        ("Books/Dune.md", "# Dune", None),
        ("Daily/today.md", "A line.", "Log"),
    ]


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_get_note_property_gives_the_value_and_a_list_of_it(
    mock_obsidian: AsyncMock,
) -> None:
    def read(held: Any) -> Any:
        mock_obsidian.get_property.return_value = held
        return asyncio.run(
            GetNoteProperty().run(
                _context(),
                GetNotePropertyInput(note="Daily/2026-05-09.md", property="habits"),
            )
        )

    listed = read(["read", "floss"])
    assert (listed.value, listed.items, listed.found, listed.note_missing) == (
        ["read", "floss"],
        ["read", "floss"],
        True,
        False,
    )
    mock_obsidian.get_property.assert_awaited_with("Daily/2026-05-09.md", "habits")
    # Anything that is not a list gives no items, so a step that needs a
    # list has one.
    text = read("read")
    assert (text.value, text.items, text.found) == ("read", [], True)
    unset = read(None)
    assert (unset.value, unset.items, unset.found) == (None, [], False)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_get_note_property_of_a_note_that_is_not_there(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.get_property.side_effect = FileNotFoundError("Daily/x.md")

    output = asyncio.run(
        GetNoteProperty().run(
            _context(), GetNotePropertyInput(note="Daily/x.md", property="habits")
        )
    )
    assert (output.value, output.items, output.found, output.note_missing) == (
        None,
        [],
        False,
        True,
    )

    with pytest.raises(FileNotFoundError):
        asyncio.run(
            GetNoteProperty().run(
                _context(),
                GetNotePropertyInput(
                    note="Daily/x.md", property="habits", if_note_missing="fail"
                ),
            )
        )


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_add_to_list_property_adds_the_value(mock_obsidian: AsyncMock) -> None:
    block = AddToListProperty()

    output = asyncio.run(
        block.run(
            _context(),
            AddToListPropertyInput(
                note="Daily/2026-05-09.md", property="habits", value="read"
            ),
        )
    )

    assert output.note == "Daily/2026-05-09.md"
    mock_obsidian.add_value_to_list_property.assert_awaited_once_with(
        "Daily/2026-05-09.md", "habits", "read"
    )
    # Named as "Set note properties" names a property, so that the page
    # can say when two workflows write the same one.
    stored = {"note": "Daily/{{ trigger.date }}.md", "property": "habits", "value": 1}
    assert block.writes(stored) == [
        "the property “habits” of Daily/{{ trigger.date }}.md"
    ]
    assert block.writes({"note": "", "property": "habits"}) == []


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_query_dataview_gives_the_rows(mock_obsidian: AsyncMock) -> None:
    rows = [{"file": {"path": "Books/Dune.md"}, "title": "Dune"}]
    mock_obsidian.query_dataview = AsyncMock(return_value=rows)

    output = asyncio.run(
        QueryDataview().run(_context(), QueryDataviewInput(query="table title"))
    )

    assert (output.rows, output.count) == (rows, 1)
    mock_obsidian.query_dataview.assert_awaited_once_with("table title")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_query_dataview_fails_for_what_is_no_table_or_list(
    mock_obsidian: AsyncMock,
) -> None:
    # What the vault gives for a query of another type, or for an error.
    mock_obsidian.query_dataview = AsyncMock(return_value={"type": "task"})

    with pytest.raises(ValueError, match="neither a table nor a list"):
        asyncio.run(QueryDataview().run(_context(), QueryDataviewInput(query="task")))


# -- the type of a property ----------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "value", "written"),
    [
        ("any", "5", "5"),
        ("text", 5, "5"),
        ("text", 1.5, "1.5"),
        ("text", True, "yes"),
        ("number", "5", 5),
        ("number", " 80.5 ", 80.5),
        ("number", 3, 3),
        ("checkbox", "yes", True),
        ("checkbox", "False", False),
        ("checkbox", True, True),
        ("date", "2026-05-09", "2026-05-09"),
        ("date", "2026-05-09 18:30:00", "2026-05-09"),
        ("date", datetime.date(2026, 5, 9), "2026-05-09"),
        ("list", ["a", "b"], ["a", "b"]),
        ("list", "a, b ,, c", ["a", "b", "c"]),
        ("list", 5, [5]),
        # Nothing stays nothing, whatever the type: it is not written.
        ("number", None, None),
    ],
)
def test_a_property_is_written_as_its_type_has_it(
    kind: str, value: Any, written: Any
) -> None:
    result = typed_property("steps", value, kind)

    assert result == written and type(result) is type(written)


@pytest.mark.parametrize(
    ("kind", "value", "said"),
    [
        ("number", "many", "a number, and the text “many” is none."),
        ("number", True, "a number, and yes is none."),
        ("checkbox", "maybe", "a checkbox, and the text “maybe” is none."),
        ("checkbox", 1, "a checkbox, and the number 1 is none."),
        ("date", "tomorrow", "a date, and the text “tomorrow” is none."),
        ("date", 5, "a date, and the number 5 is none."),
        ("text", ["a"], "a text, and a list of 1 is none."),
    ],
)
def test_a_value_that_is_not_of_the_type_says_so(
    kind: str, value: Any, said: str
) -> None:
    with pytest.raises(ValueError) as error:
        typed_property("steps", value, kind)

    assert str(error.value) == f"The property “steps” is {said}"


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_set_note_properties_writes_each_property_as_its_type(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.get_property.return_value = None

    output = _set(
        mock_obsidian,
        properties={"steps": "6227", "done": "yes", "note": "7", "gone": None},
        types={"steps": "number", "done": "checkbox"},
    )

    # A property without a type is written as it comes.
    assert _sets(mock_obsidian) == [
        (NOTE, "steps", 6227),
        (NOTE, "done", True),
        (NOTE, "note", "7"),
    ]
    assert output.written == ["steps", "done", "note"]


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_a_property_of_the_wrong_type_fails_the_step_before_anything_is_written(
    mock_obsidian: AsyncMock,
) -> None:
    with pytest.raises(ValueError, match="The property “steps” is a number"):
        _set(
            mock_obsidian,
            properties={"done": "yes", "steps": "many"},
            types={"steps": "number"},
        )

    mock_obsidian.set_value_of_property.assert_not_awaited()


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_write_note_writes_each_property_as_its_type(mock_obsidian: AsyncMock) -> None:
    inputs = WriteNoteInput(
        path="Sets/a.md",
        properties={"reps": "5", "warmup": "no", "tags": "a, b"},
        types={"reps": "number", "warmup": "checkbox", "tags": "list"},
    )

    asyncio.run(WriteNote().run(_context(), inputs))

    text = mock_obsidian.write_binary_file.await_args.args[1].decode("utf-8")
    assert text == "---\nreps: 5\nwarmup: false\ntags:\n- a\n- b\n---\n\n"


# -- the files of the vault -----------------------------------------------------

SCANS = [
    "Assets/scans/2026-05-02 Scan.pdf",
    "Assets/scans/2026-05-01 Scan 2.pdf",
    "Assets/scans/2026-04-30 Water bill.pdf",
    "Assets/scans/2026-05-03 Scan.PDF",
    "Assets/scans/notes.md",
    "Assets/scans/old/2026-01-01 Scan.pdf",
    "Assets/scans-of-others/2026-05-01 Scan.pdf",
]


def _list(mock_obsidian: AsyncMock, **inputs: Any) -> ListFilesOutput:
    mock_obsidian.list_dir.return_value = list(SCANS)
    values: dict[str, Any] = {"folder": "Assets/scans"}
    values.update(inputs)
    return asyncio.run(ListFiles().run(_context(), ListFilesInput(**values)))


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_list_files_gives_the_files_of_the_folder_itself_by_name(
    mock_obsidian: AsyncMock,
) -> None:
    output = _list(mock_obsidian, folder="/Assets/scans/")

    mock_obsidian.list_dir.assert_awaited_once_with("Assets/scans")
    assert [file.name for file in output.files] == [
        "2026-04-30 Water bill.pdf",
        "2026-05-01 Scan 2.pdf",
        "2026-05-02 Scan.pdf",
        "2026-05-03 Scan.PDF",
        "notes.md",
    ]
    assert output.count == 5
    assert output.first == "Assets/scans/2026-04-30 Water bill.pdf"
    first = output.files[0]
    assert (first.stem, first.extension) == ("2026-04-30 Water bill", ".pdf")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_list_files_can_ask_for_a_name_an_extension_and_leave_paths_out(
    mock_obsidian: AsyncMock,
) -> None:
    output = _list(
        mock_obsidian,
        pattern=r"^\d{4}-\d{2}-\d{2} Scan",
        extension=".pdf",
        leave_out=["Assets/scans/2026-05-01 Scan 2.pdf"],
    )

    assert [file.path for file in output.files] == [
        "Assets/scans/2026-05-02 Scan.pdf",
        "Assets/scans/2026-05-03 Scan.PDF",
    ]
    assert output.first == "Assets/scans/2026-05-02 Scan.pdf"


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_list_files_with_nothing_found_has_no_first(mock_obsidian: AsyncMock) -> None:
    output = _list(mock_obsidian, extension="png")

    assert (output.files, output.count, output.first) == ([], 0, "")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_list_files_says_when_the_pattern_is_no_regular_expression(
    mock_obsidian: AsyncMock,
) -> None:
    with pytest.raises(ValueError, match="no regular expression"):
        _list(mock_obsidian, pattern="(Scan")


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_read_file_is_the_file_with_its_name_and_type(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.read_binary_file.return_value = b"%PDF-1.7"

    file = asyncio.run(
        ReadFile().run(
            _context(), ReadFileInput(path="Assets/scans/2026-05-02 Scan.pdf")
        )
    )

    mock_obsidian.read_binary_file.assert_awaited_once_with(
        "Assets/scans/2026-05-02 Scan.pdf"
    )
    assert (file.name, file.stem, file.extension) == (
        "2026-05-02 Scan.pdf",
        "2026-05-02 Scan",
        ".pdf",
    )
    assert (file.type, file.data, file.size) == ("application/pdf", b"%PDF-1.7", 8)


def _move(mock_obsidian: AsyncMock, **inputs: Any) -> MoveFileOutput:
    values: dict[str, Any] = {
        "path": "Assets/scans/2026-05-02 Scan.pdf",
        "to": "Assets/scans/2026-05-02 Water bill.pdf",
    }
    values.update(inputs)
    return asyncio.run(MoveFile().run(_context(), MoveFileInput(**values)))


def _taken(*paths: str) -> Any:
    async def move(from_path: str, to_path: str) -> None:
        if to_path in paths:
            raise FileExistsError(to_path)

    return move


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_move_file_moves_the_file_and_says_where_it_is(
    mock_obsidian: AsyncMock,
) -> None:
    output = _move(mock_obsidian)

    mock_obsidian.move_file.assert_awaited_once_with(
        "Assets/scans/2026-05-02 Scan.pdf", "Assets/scans/2026-05-02 Water bill.pdf"
    )
    assert (output.path, output.moved) == (
        "Assets/scans/2026-05-02 Water bill.pdf",
        True,
    )


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_move_file_fails_when_there_is_a_file_there(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.move_file.side_effect = _taken(
        "Assets/scans/2026-05-02 Water bill.pdf"
    )

    with pytest.raises(FileExistsError, match="2026-05-02 Water bill.pdf already"):
        _move(mock_obsidian)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_move_file_can_leave_the_file_where_it_is(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.move_file.side_effect = _taken(
        "Assets/scans/2026-05-02 Water bill.pdf"
    )

    output = _move(mock_obsidian, if_exists="skip")

    assert (output.path, output.moved) == ("Assets/scans/2026-05-02 Scan.pdf", False)


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_move_file_can_add_a_number_to_a_name_that_is_taken(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.move_file.side_effect = _taken(
        "Assets/scans/2026-05-02 Water bill.pdf",
        "Assets/scans/2026-05-02 Water bill 2.pdf",
    )

    output = _move(mock_obsidian, if_exists="add a number")

    assert (output.path, output.moved) == (
        "Assets/scans/2026-05-02 Water bill 3.pdf",
        True,
    )


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_move_file_adds_the_number_to_a_name_without_an_extension(
    mock_obsidian: AsyncMock,
) -> None:
    mock_obsidian.move_file.side_effect = _taken("Archive.v2/Bill")

    output = _move(mock_obsidian, to="Archive.v2/Bill", if_exists="add a number")

    assert output.path == "Archive.v2/Bill 2"


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_delete_file_deletes_the_file(mock_obsidian: AsyncMock) -> None:
    output = asyncio.run(
        DeleteFile().run(_context(), DeleteFileInput(path="Assets/scans/a.pdf"))
    )

    mock_obsidian.delete_file.assert_awaited_once_with("Assets/scans/a.pdf")
    assert output.deleted is True


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_delete_file_of_a_file_that_is_not_there(mock_obsidian: AsyncMock) -> None:
    mock_obsidian.delete_file.side_effect = FileNotFoundError("a.pdf")

    output = asyncio.run(
        DeleteFile().run(_context(), DeleteFileInput(path="Assets/scans/a.pdf"))
    )
    assert output.deleted is False

    with pytest.raises(FileNotFoundError):
        asyncio.run(
            DeleteFile().run(
                _context(),
                DeleteFileInput(path="Assets/scans/a.pdf", if_missing="fail"),
            )
        )
