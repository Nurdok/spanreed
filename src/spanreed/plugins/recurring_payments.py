"""What is left of Recurring Payments: how a payment becomes a workflow.

The plugin kept, for each payment that comes up every week, a Todoist task
that lists the dates not paid yet and says what they come to. At the time
of a payment it added the date, after asking if the user wanted that, and
logged it to a note. It is gone: each payment a user had is a workflow of
its own now, made here from the payment as the plugin saved it. The recipe
"Recurring payment" is the same workflow for a payment that is new.
"""

import json
import logging
from typing import Any

from spanreed.interaction import UserInteractionApi
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.workflows.builtin import WEEKDAYS
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, TriggerSpec, Workflow, new_workflow_id

_logger = logging.getLogger(__name__)

CANONICAL_NAME = "recurring-payments"
# What ``recipe`` holds for a workflow that was a payment: this, and the
# place of the payment in the user's list, from 1.
PAYMENT_RECIPE = "recurring-payment"

# A template of the plugin knew the dates and what they cost by these
# names, and the template of the note knew the date. In the workflow they
# are values of steps. Starting a template with these keeps the names.
TASK_NAMES = "{% set dates = tally.dates_text %}{% set total_cost = tally.total_cost %}"
NOTE_NAMES = "{% set date = trigger.date %}"
# The name under which a workflow remembers that the note has no entry yet
# for a date that it added to the task.
TO_LOG = "to-log:{{ trigger.date }}"


def workflow_for_payment(
    workflow_id: str, user_id: int, payment: dict[str, Any], place: int
) -> Workflow:
    """Return the workflow that does what the plugin did for a payment.

    ``payment`` is the payment as the plugin saved it, and ``place`` is its
    place in the user's list, from 1. The command of the workflow is
    hidden: the plugin had none.
    """
    label = str(payment.get("todoist_label") or "")
    cost = payment.get("recurrence_cost") or 0
    when = payment.get("recurrence_info")
    when = when if isinstance(when, dict) else {}
    weekday = when.get("week_day")
    on = WEEKDAYS[weekday] if isinstance(weekday, int) and 0 <= weekday < 7 else None
    note = payment.get("obsidian_log")

    adding: list[Step] = []
    if payment.get("verify_recurrence"):
        adding += [
            Step(
                id="sure",
                block="core.ask-choice",
                inputs={
                    "question": (
                        f"You have a recurring payment of {float(cost)} due now "
                        f"(for task with label {label}).\n"
                        "Would you like to add it to the list of dates?"
                    ),
                    "options": ["Yes (add)", "No (skip)"],
                    "cancel": "",
                    # The plugin asked ahead of every other conversation.
                    "priority": "high",
                },
            ),
            Step(
                id="yes",
                block="core.continue-if",
                inputs={"condition": "{{ sure.answer == 'Yes (add)' }}"},
            ),
        ]
    adding.append(
        Step(
            id="write",
            block="todoist.write-tally",
            inputs={
                "label": label,
                "dates": "{{ tally.new_dates }}",
                "name": TASK_NAMES + str(payment.get("todoist_task_template") or ""),
                "project": payment.get("todoist_project_id") or None,
            },
        )
    )
    if isinstance(note, dict):
        # The note is owed an entry from the moment the date is on the
        # task. Remembered, so that a run that fails at the note writes it
        # when it is tried again, where the date is on the task already.
        adding.append(Step(id="owe", block="core.remember", inputs={"name": TO_LOG}))
    steps = [
        Step(
            id="tally",
            block="todoist.read-tally",
            inputs={"label": label, "date": "{{ trigger.date }}", "cost": cost},
        ),
        Step(
            id="new",
            block="core.if",
            inputs={"condition": "{{ not tally.has_date }}"},
            steps=adding,
        ),
    ]
    if isinstance(note, dict):
        folder = str(note.get("file_location") or "").strip("/")
        path = f"{folder + '/' if folder else ''}{note.get('note_title') or ''}.md"
        steps += [
            Step(id="owed", block="core.recall", inputs={"name": TO_LOG}),
            Step(
                id="due",
                block="core.continue-if",
                inputs={"condition": "{{ owed.found }}"},
            ),
            Step(
                id="log",
                block="obsidian.append-to-note",
                inputs={
                    "path": path,
                    "text": NOTE_NAMES + str(note.get("note_content_template") or ""),
                },
            ),
            Step(id="logged", block="core.forget", inputs={"name": TO_LOG}),
        ]
    return Workflow(
        id=workflow_id,
        user_id=user_id,
        name=f"Recurring payment: {label}" if label else f"Recurring payment {place}",
        trigger=TriggerSpec(
            block="core.at-time",
            params={
                "time": f"{int(when.get('hour') or 0):02d}:{int(when.get('minute') or 0):02d}",
                "on": on or "every day",
                "timezone": str(when.get("timezone") or ""),
            },
        ),
        steps=steps,
        # A payment whose day is not one cannot have run for the plugin either.
        enabled=on is not None,
        command_hidden=True,
        recipe=f"{PAYMENT_RECIPE}-{place}",
    )


async def move(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the plugin a workflow for each payment they had.

    Return the workflows that were made. Doing this again makes nothing
    new. A workflow that the engine cannot run is stored turned off, and
    says why on its page.
    """
    raw = await redis_api.get(f"config:plugin:name={CANONICAL_NAME}:user_id={user.id}")
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    payments = saved.get("recurring_payments") if isinstance(saved, dict) else None
    existing = {w.recipe for w in await engine.store.list_for_user(user.id)}
    made = []
    for place, payment in enumerate(payments or [], start=1):
        if not isinstance(payment, dict) or f"{PAYMENT_RECIPE}-{place}" in existing:
            continue
        workflow = workflow_for_payment(new_workflow_id(), user.id, payment, place)
        if engine.problems(workflow):
            workflow.enabled = False
        await engine.save(workflow)
        made.append(workflow)
    if made:
        _logger.info(
            f"Moved the recurring payments of user {user.id} to workflows"
            f" {[workflow.id for workflow in made]}."
        )
        await _tell(user, made)
    return made


async def _tell(user: User, made: list[Workflow]) -> None:
    quoted = [f"“{workflow.name}”" for workflow in made]
    names = (
        " and ".join(quoted)
        if len(quoted) < 3
        else ", ".join(quoted[:-1]) + " and " + quoted[-1]
    )
    text = (
        "The plugin “Recurring Payments” is now "
        f"{'a workflow' if len(made) == 1 else 'workflows'}, one for each "
        f"payment: {names}. They do what the plugin did, at the same times. "
        "Change a payment, or its time, under Workflows in the web UI. For a "
        "new payment, add the recipe “Recurring payment”."
    )
    off = [workflow.name for workflow in made if not workflow.enabled]
    if off:
        text += f" “{off[0]}” is off: its page says what is wrong with it."
    bot = await UserInteractionApi.for_user(user)
    await bot.notify(text, parse_html=False)
