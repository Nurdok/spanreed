import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from spanreed.apis.obsidian_blocks import (
    CreateDailyNote,
    SetNoteProperties,
    SetNotePropertiesInput,
    SetNotePropertiesOutput,
    WriteFile,
    WriteFileInput,
    WriteFileOutput,
    obsidian_blocks,
)
from spanreed.test_utils import mock_user_find_by_id, patch_obsidian
from spanreed.workflows.blocks import BlockRegistry, NoInput, RunInfo, StepContext
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
        "obsidian.set-note-properties",
        "obsidian.write-file",
    ]


@patch_obsidian("spanreed.apis.obsidian_blocks")
def test_create_daily_note_asks_obsidian_for_todays_note(
    mock_obsidian: AsyncMock,
) -> None:
    asyncio.run(CreateDailyNote().run(_context(), NoInput()))

    mock_obsidian.safe_generate_today_note.assert_awaited_once()


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
