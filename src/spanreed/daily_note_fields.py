"""Let a user choose which properties a plugin writes into the daily note.

A plugin that writes several daily note properties keeps the choice in its
config as a dataclass of yes/no fields, one for each property. The web
settings form shows such a dataclass as a group of checkboxes. This module
holds what those dataclasses share, and the chat question that edits one.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from spanreed.interaction import UserInteractionApi

T = TypeVar("T", bound="DailyNoteFieldsBase")


class DailyNoteFieldsBase:
    """Base of a dataclass that says which daily note properties are written.

    Each field of the subclass is one property and holds a ``bool``. The
    field name is the property name with ``_`` for ``-``. The fields are in
    the order the properties are written.
    """

    @classmethod
    def properties(cls) -> list[str]:
        """Return every property name, in the order it is written."""
        return [
            field.name.replace("_", "-")
            for field in dataclasses.fields(cls)  # type: ignore[arg-type]
        ]

    @classmethod
    def from_saved(cls: type[T], data: dict[str, Any]) -> T:
        """Build the fields from saved JSON data.

        Ignore a property that no longer exists. A property that is not in
        the data keeps its default.
        """
        known = {field.name for field in dataclasses.fields(cls)}  # type: ignore[arg-type]
        return cls(
            **{name: bool(value) for name, value in data.items() if name in known}
        )

    def writes(self, prop: str) -> bool:
        """Return whether the daily note property ``prop`` is written."""
        return bool(getattr(self, prop.replace("-", "_")))

    def set(self, prop: str, write: bool) -> None:
        """Turn the daily note property ``prop`` on or off."""
        setattr(self, prop.replace("-", "_"), write)

    def written(self) -> list[str]:
        """Return the names of the properties that are written."""
        return [prop for prop in self.properties() if self.writes(prop)]


async def ask_for_daily_note_fields(
    bot: UserInteractionApi, fields: DailyNoteFieldsBase
) -> None:
    """Ask which properties to write into the daily note.

    Change ``fields`` in place. Let the user turn properties on and off one
    at a time until they are done.
    """
    properties = fields.properties()
    choice = await bot.request_user_choice(
        f"{len(fields.written())} of {len(properties)} daily note "
        "properties are written.",
        ["Keep these", "Choose properties"],
    )
    if choice != 1:  # Keep these
        return
    while True:
        choice = await bot.request_user_choice(
            "Choose a property to turn it on or off. Turning one off "
            "does not remove values already in your notes.",
            [f"{'✅' if fields.writes(prop) else '⬜'} {prop}" for prop in properties]
            + ["Done"],
        )
        if not 0 <= choice < len(properties):  # Done
            return
        fields.set(properties[choice], not fields.writes(properties[choice]))
