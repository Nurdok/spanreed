"""Recipes: the workflows that ship with Spanreed.

A recipe is a JSON file in ``recipes/``. Installing one copies it into the
user's workflows, where it is theirs to change. Recipes are how a behavior
that used to be a plugin arrives as a workflow.
"""

from __future__ import annotations

import functools
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from spanreed.workflows.model import Step, TriggerSpec, Workflow, new_workflow_id

_RECIPES_DIR = pathlib.Path(__file__).parent / "recipes"


@dataclass(frozen=True)
class Recipe:
    """A workflow that a user can install."""

    id: str
    name: str
    description: str
    trigger: dict[str, Any]
    steps: list[dict[str, Any]]
    # Canonical names of the plugins whose blocks the recipe uses.
    requires: list[str] = field(default_factory=list)
    command_name: str | None = None
    command_hidden: bool = False

    def workflow_for(self, user_id: int) -> Workflow:
        """Return a new workflow of a user, made from this recipe."""
        return Workflow(
            id=new_workflow_id(),
            user_id=user_id,
            name=self.name,
            trigger=TriggerSpec(**self.trigger),
            steps=[Step(**step) for step in self.steps],
            command_name=self.command_name,
            command_hidden=self.command_hidden,
            recipe=self.id,
        )


@functools.cache
def all_recipes() -> dict[str, Recipe]:
    """Return every recipe that ships with Spanreed, by id."""
    recipes = {}
    for path in sorted(_RECIPES_DIR.glob("*.json")):
        recipe = Recipe(**json.loads(path.read_text(encoding="utf-8")))
        recipes[recipe.id] = recipe
    return recipes
