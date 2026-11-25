"""What is left of the Habit Tracker plugin: how its settings become a workflow.

The plugin asked, every hour, which of the user's habits were done today, and
marked each one in a property of the daily note. The recipe "Habit check-in"
does that now, and the plugin is gone. ``keep_settings`` makes the recipe ask
about the habits the user had, and write where the plugin wrote.
"""

from collections.abc import Callable
from typing import Any

from spanreed.workflows.model import Step, Workflow

# The recipe that does what the plugin did.
RECIPE = "habit-check-in"
# What the recipe ships with, which a user of the plugin had their own of.
DEFAULT_PROPERTY = "habits"
# The plugin laid the habits out in at least this many columns.
MIN_COLUMNS = 4


def options_for(names: list[str]) -> dict[str, str]:
    """Return the options of the question: what a button says, and the habit.

    The plugin showed a habit with a capital first letter and the rest in
    small letters, and wrote it to the note as the user had named it.
    """
    options: dict[str, str] = {}
    for name in names:
        shown = name.capitalize()
        # Two habits that would read the same keep their own names.
        options[name if shown in options else shown] = name
    return options


def keep_settings(saved: dict[str, Any] | None) -> Callable[[Workflow], None]:
    """Return what makes a new workflow ask and write as the plugin did.

    ``saved`` is the plugin's saved config: the folder of the daily notes,
    the name of the property, and the habits. Without one, or without
    habits, the workflow is the recipe as it ships, turned off: its habits
    are examples, and nobody wants to be asked about those every hour.
    """
    config = saved or {}
    names = [
        str(habit.get("name") or "")
        for habit in config.get("habits") or []
        if isinstance(habit, dict) and habit.get("name")
    ]
    folder = str(config.get("daily_note_path") or "").strip("/")
    name = str(config.get("habit_tracker_property_name") or "")

    # The plugin wrote to the note that it named itself, from its own
    # setting. The recipe asks Obsidian where the daily note is. A workflow
    # that takes the place of the plugin writes where the plugin did.
    note = (f"{folder}/" if folder else "") + "{{ trigger.date }}.md"

    def change(steps: list[Step]) -> None:
        for step in steps:
            inputs = step.inputs
            if isinstance(inputs.get("note"), str):
                inputs["note"] = note
            if inputs.get("property") == DEFAULT_PROPERTY and name:
                inputs["property"] = name
            if step.block == "core.ask-each":
                inputs["options"] = options_for(names)
                inputs["columns"] = max(MIN_COLUMNS, len(names))
            change(step.steps)

    def customize(workflow: Workflow) -> None:
        if not names:
            workflow.enabled = False
            return
        change(workflow.steps)

    return customize
