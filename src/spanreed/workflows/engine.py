"""Run workflows: start their triggers, run their steps, keep the traces.

One event of a trigger is one run. The runs of a workflow go one at a time.
A run fills each step's inputs from the outputs before it, calls the step's
block, and stops at the first step that fails or that ends the run. A step
whose block is a loop runs the steps inside it once for each round. What
each step received and returned is kept as the run's trace.

A run that fails is tried again for the same event, from its first step,
after a pause that doubles each time. So a step can run more than once for
one event, and a block must be safe to repeat. A workflow whose runs keep
failing, because a service is down for hours, is then tried only once every
half hour until a run works.

The engine never writes a step's inputs or outputs to the log: they can hold
anything the user's services know. They are kept in the trace only.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import logging
import random
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Any, cast

from telegram.error import RetryAfter

from spanreed.interaction import PluginCommand, SurfaceBusyError, UserInteractionApi
from spanreed.interaction import commands
from spanreed.user import User
from spanreed.workflows import expressions, model
from spanreed.workflows.blocks import (
    Action,
    BlockRegistry,
    Loop,
    ManualError,
    NoQuestion,
    RunInfo,
    StepContext,
    StopRun,
    Trigger,
    TriggerContext,
    manual_questions,
)
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.recipes import Recipe
from spanreed.workflows.store import WorkflowStore
from spanreed.workflows.values import from_data, from_whole_data, to_data, whole_data

# How a run, or a step of it, ended.
OK = "ok"
FINISHED = "finished"
STOPPED = "stopped"
FAILED = "failed"
CANCELLED = "cancelled"

# Indirections so tests can fake the clock and the waits without patching
# time.monotonic and asyncio.sleep globally (asyncio and the command
# registry use them too).
_monotonic = time.monotonic
_sleep = asyncio.sleep

# What ``anext`` gives when a trigger has no more events.
_NO_MORE = object()


class _RoundFailed(Exception):
    """A step inside a loop failed. The message says where and why."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def outcome(trace: dict[str, Any]) -> str:
    """Return how a run ended, as the end of a sentence about it."""
    status = trace["status"]
    if status == FINISHED:
        return "finished."
    if status == CANCELLED:
        return "was cancelled."
    step = trace["steps"][-1]
    if status == STOPPED:
        return f"stopped at {_where(trace)}."
    return f"failed at {_where(trace)}: {step['error']}"


def _where(trace: dict[str, Any]) -> str:
    """Return the last step that a run got to: its number and its title."""
    return f"step {len(trace['steps'])}, {trace['steps'][-1]['title']}"


class WorkflowEngine:
    """Runs the workflows of every user."""

    # A failed run is tried this many times in all for one event. The pause
    # before the next try starts at the first value and doubles up to the
    # second, like the restarts of a plugin that crashed.
    MAX_ATTEMPTS = 5
    RETRY_INITIAL_S = 60.0
    RETRY_MAX_S = 1800.0
    # A trigger that ran this long before it failed starts its pauses over.
    RETRY_RESET_UPTIME_S = 600.0
    # During Telegram flood control a step waits and is tried again, this
    # many times. The random extra wait spreads out the steps that were
    # stopped together (see ``Plugin._run_for_user_supervised``).
    FLOOD_MAX_WAITS = 3
    FLOOD_WAKE_JITTER_RANGE_S = (60.0, 600.0)
    # The trace of a loop keeps this many of its rounds: the first ones,
    # and one that failed.
    ROUNDS_KEPT = 50

    def __init__(
        self,
        registry: BlockRegistry,
        store: WorkflowStore | None = None,
        command_plugin: str = "workflows",
    ) -> None:
        """Create an engine for the blocks in ``registry``.

        ``command_plugin`` is the canonical name of the plugin that the
        workflows' commands are listed under.
        """
        self._registry = registry
        self._store = store or WorkflowStore()
        self._command_plugin = command_plugin
        self._sources: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Workflows whose user was told that they fail, until a run works,
        # each with the time of its last failed try.
        self._failing: dict[str, float] = {}
        self._logger = logging.getLogger("spanreed.workflows.engine")

    @property
    def registry(self) -> BlockRegistry:
        """Return the blocks that workflows can use."""
        return self._registry

    @property
    def store(self) -> WorkflowStore:
        """Return where the workflows and their traces are kept."""
        return self._store

    @property
    def command_plugin(self) -> str:
        """Return the canonical name of the plugin that owns the engine."""
        return self._command_plugin

    # -- what runs --------------------------------------------------------

    async def start(self, user_ids: Iterable[int]) -> None:
        """Start the stored workflows of these users."""
        for user_id in user_ids:
            for workflow in await self._store.list_for_user(user_id):
                self._apply(workflow)

    async def stop(self) -> None:
        """Stop every trigger, and any run that is in progress."""
        tasks = list(self._sources.values())
        self._sources.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def install(
        self,
        user: User,
        recipe: Recipe,
        customize: Callable[[Workflow], None] | None = None,
    ) -> Workflow:
        """Give a user a new workflow made from a recipe, and start it.

        ``customize`` can change the workflow before it is checked and
        stored, for example to carry over the settings of a plugin. Raise
        ``ValueError`` if the workflow does not fit the blocks that are
        loaded.
        """
        workflow = recipe.workflow_for(user.id)
        if customize is not None:
            customize(workflow)
        found = self.problems(workflow)
        if found:
            raise ValueError(f"Recipe {recipe.id} cannot be installed: {found}")
        await self.save(workflow)
        return workflow

    async def save(self, workflow: Workflow) -> None:
        """Store a workflow and make what runs match it."""
        await self._store.save(workflow)
        self._apply(workflow)

    async def delete(self, workflow: Workflow) -> None:
        """Stop a workflow and delete it with its history."""
        self._stop_source(workflow.id)
        commands.unregister_command(self._command_id(workflow.id))
        self._failing.pop(workflow.id, None)
        self._locks.pop(workflow.id, None)
        await self._store.delete(workflow)

    def problems(self, workflow: Workflow) -> list[str]:
        """Return what keeps a workflow from running, if anything."""
        return model.problems(workflow, self._registry)

    def can_run_by_hand(self, workflow: Workflow) -> bool:
        """Return whether the workflow's trigger can be started by hand."""
        trigger = self._registry.get(workflow.trigger.block)
        return isinstance(trigger, Trigger) and trigger.Manual is not None

    def _command_id(self, workflow_id: str) -> str:
        # The id of the workflow, not its name, so that a rename does not
        # change the command for surfaces that remember commands.
        return f"workflow.{workflow_id}"

    def _apply(self, workflow: Workflow) -> None:
        """Make the running trigger and the command match a workflow."""
        self._stop_source(workflow.id)
        commands.unregister_command(self._command_id(workflow.id))
        if not workflow.enabled:
            return
        found = self.problems(workflow)
        if found:
            self._logger.error(f"Workflow {workflow.id} is not started: {found}")
            return
        if self.can_run_by_hand(workflow) and not workflow.command_hidden:
            commands.register_command(
                self._command_plugin,
                PluginCommand(
                    text=workflow.command_text,
                    callback=functools.partial(self.run_by_hand, workflow.id),
                    id=self._command_id(workflow.id),
                ),
                user_id=workflow.user_id,
            )
        self._sources[workflow.id] = asyncio.create_task(
            self._supervise(workflow), name=f"workflow-{workflow.id}"
        )

    def _stop_source(self, workflow_id: str) -> None:
        task = self._sources.pop(workflow_id, None)
        if task is not None and not task.done():
            task.cancel()

    # -- triggers ---------------------------------------------------------

    def _trigger_of(self, workflow: Workflow) -> Trigger:
        return cast(Trigger, self._registry.get(workflow.trigger.block))

    def _trigger_context(self, workflow: Workflow, user: User) -> TriggerContext:
        return TriggerContext(
            user=user,
            workflow_id=workflow.id,
            state=self._store.trigger_state(workflow.id),
            queue=self._store.queue(workflow.id),
        )

    async def _supervise(self, workflow: Workflow) -> None:
        """Run a workflow for each event of its trigger.

        Start the trigger again, after a pause, if it fails. A workflow
        that waits for a service that is down must not turn into a tight
        loop, so the pause doubles each time.
        """
        delay = self.RETRY_INITIAL_S
        while True:
            started = _monotonic()
            try:
                await self._consume(workflow)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if _monotonic() - started > self.RETRY_RESET_UPTIME_S:
                    delay = self.RETRY_INITIAL_S
                self._logger.exception(
                    f"The trigger of workflow {workflow.id} failed;"
                    f" starting it again in {delay:.0f}s."
                )
                await _sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX_S)

    async def _consume(self, workflow: Workflow) -> None:
        trigger = self._trigger_of(workflow)
        params = from_data(trigger.Params, workflow.trigger.params)
        user = await User.find_by_id(workflow.user_id)
        events = trigger.events(self._trigger_context(workflow, user), params)
        try:
            event = await anext(events, _NO_MORE)
            while event is not _NO_MORE:
                worked = await self._run_until_done(workflow, event)
                try:
                    # Tell the trigger how it went, and take its next event.
                    event = await events.asend(worked)
                except StopAsyncIteration:
                    break
        finally:
            await events.aclose()

    async def _run_until_done(self, workflow: Workflow, event: Any) -> bool:
        """Run a workflow for an event, again if the run fails.

        Return whether a run worked. A run that a block ended on purpose
        worked.

        A workflow that already failed every try for an earlier event gets
        one try for this one, and no sooner than ``RETRY_MAX_S`` after its
        last failure. So a service that is down for hours is asked twice an
        hour, as by a plugin that keeps crashing, until a run works.
        """
        if workflow.id in self._failing:
            wait = self._failing[workflow.id] + self.RETRY_MAX_S - _monotonic()
            if wait > 0:
                await _sleep(wait)
            user = await User.find_by_id(workflow.user_id)
            trace = await self._run(workflow, user, event, manual=False, attempt=1)
            if trace["status"] == FAILED:
                self._failing[workflow.id] = _monotonic()
                return False
            del self._failing[workflow.id]
            return True

        delay = self.RETRY_INITIAL_S
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            # Loaded for each try: the user's settings may have changed.
            user = await User.find_by_id(workflow.user_id)
            trace = await self._run(
                workflow, user, event, manual=False, attempt=attempt
            )
            if trace["status"] != FAILED:
                return True
            if attempt < self.MAX_ATTEMPTS:
                await _sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX_S)
        await self._tell_about_failure(workflow, user, trace)
        return False

    async def _tell_about_failure(
        self, workflow: Workflow, user: User, trace: dict[str, Any]
    ) -> None:
        """Tell the user that a workflow fails, once until it works again."""
        self._failing[workflow.id] = _monotonic()
        try:
            bot = await UserInteractionApi.for_user(user)
            await bot.notify(
                f"The workflow “{workflow.name}” {outcome(trace)}\n"
                f"It was tried {self.MAX_ATTEMPTS} times, and will be tried "
                "again.",
                parse_html=False,
            )
        except Exception:
            self._logger.exception(
                f"Could not tell user {user.id} that workflow {workflow.id} fails."
            )

    # -- runs -------------------------------------------------------------

    async def _run(
        self,
        workflow: Workflow,
        user: User,
        event: Any,
        *,
        manual: bool,
        attempt: int,
        test: bool = False,
    ) -> dict[str, Any]:
        """Run the steps of a workflow for one event, and return the trace.

        ``test`` marks a run that the editor made to try a workflow out.
        """
        run = RunInfo(id=secrets.token_hex(6), manual=manual)
        ctx = StepContext(user=user, workflow_id=workflow.id, run=run)
        context: dict[str, Any] = {model.TRIGGER_NAME: event, model.RUN_NAME: run}
        trace: dict[str, Any] = {
            "id": run.id,
            "workflow_id": workflow.id,
            "started_at": _now(),
            "manual": manual,
            "attempt": attempt,
            "trigger": {
                "block": workflow.trigger.block,
                "outputs": to_data(event),
            },
            "steps": [],
            "status": FINISHED,
        }
        if test:
            trace["test"] = True
        else:
            await self._keep_event(workflow, trace["started_at"], event)
        async with self._locks.setdefault(workflow.id, asyncio.Lock()):
            try:
                trace["status"] = await self._run_steps(
                    workflow.steps, ctx, context, trace["steps"]
                )
            except asyncio.CancelledError:
                trace["status"] = CANCELLED
                raise
            finally:
                trace["finished_at"] = _now()
                await self._keep(workflow, trace)
        return trace

    async def _keep(self, workflow: Workflow, trace: dict[str, Any]) -> None:
        self._logger.info(
            f"Workflow {workflow.id}, run {trace['id']}: {trace['status']}"
            f" after {len(trace['steps'])} of {len(workflow.steps)} steps."
        )
        try:
            await self._store.add_run(workflow.id, trace)
        except Exception:
            self._logger.exception(
                f"Could not keep the trace of run {trace['id']}"
                f" of workflow {workflow.id}."
            )

    async def _keep_event(self, workflow: Workflow, at: str, event: Any) -> None:
        """Keep the event of a run, whole, for the editor to test with."""
        try:
            await self._store.keep_last_event(workflow.id, at, whole_data(event))
        except Exception:
            self._logger.exception(
                f"Could not keep the last event of workflow {workflow.id}."
            )

    async def _run_steps(
        self,
        steps: list[Step],
        ctx: StepContext,
        context: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> str:
        """Run steps in order, up to one that fails or ends the run.

        Add the entry of each step that ran to ``entries``, as it ends, so
        that a run that is cancelled keeps the steps it got through. Return
        how the steps ended: ``FINISHED``, or the status of the last one.
        """
        for step in steps:
            entry = await self._run_step(step, ctx, context)
            entries.append(entry)
            if entry["status"] != OK:
                return str(entry["status"])
        return FINISHED

    async def _run_step(
        self, step: Step, ctx: StepContext, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one step, and return its entry in the trace.

        Add the step's outputs to ``context`` for the steps after it.
        """
        block = cast(Action, self._registry.get(step.block))
        entry: dict[str, Any] = {
            "id": step.id,
            "block": step.block,
            "title": block.title,
            "status": OK,
        }
        started = _monotonic()
        try:
            plugin = self._registry.plugin_of(step.block)
            if not await plugin.is_registered(ctx.user):
                raise RuntimeError(
                    f"{plugin.name()} is not set up. Register to it with "
                    "“Manage plugins”."
                )
            inputs = from_data(block.Input, expressions.render(step.inputs, context))
            entry["inputs"] = to_data(inputs)
            if isinstance(block, Loop):
                await self._run_rounds(step, block, inputs, ctx, context, entry)
            outputs = await self._call(block, ctx, inputs)
            context[step.id] = outputs
            entry["outputs"] = to_data(outputs)
        except StopRun as stop:
            entry["status"] = STOPPED
            entry["reason"] = stop.reason
        except _RoundFailed as failed:
            # The step inside wrote the failure to the log already.
            entry["status"] = FAILED
            entry["error"] = str(failed)
        except Exception as e:
            entry["status"] = FAILED
            entry["error"] = str(e) or type(e).__name__
            self._logger.exception(
                f"Workflow {ctx.workflow_id}, run {ctx.run.id}:"
                f" step {step.id} ({step.block}) failed."
            )
        entry["duration_ms"] = round((_monotonic() - started) * 1000)
        return entry

    async def _run_rounds(
        self,
        step: Step,
        loop: Loop,
        inputs: Any,
        ctx: StepContext,
        context: dict[str, Any],
        entry: dict[str, Any],
    ) -> None:
        """Run the steps inside a loop once for each of its rounds.

        In a round, the id of the loop's step refers to the value of the
        round. A step that ends the run ends only its round. Raise
        ``_RoundFailed`` at the first step that fails.

        Add the rounds to the loop's ``entry`` of the trace.
        """
        values = loop.rounds(inputs)
        entry["round_name"] = loop.round_name
        entry["round_count"] = len(values)
        entry["rounds_run"] = 0
        entry["rounds"] = []
        for number, value in enumerate(values, start=1):
            entry["rounds_run"] = number
            steps: list[dict[str, Any]] = []
            status = await self._run_steps(
                step.steps, ctx, {**context, step.id: value}, steps
            )
            kept = {"number": number, "status": status, "steps": steps}
            if len(entry["rounds"]) < self.ROUNDS_KEPT or status == FAILED:
                entry["rounds"].append(kept)
            if status == FAILED:
                failed = steps[-1]
                raise _RoundFailed(
                    f"{loop.round_name} {number} of {len(values)}, {failed['title']}: "
                    f"{failed['error']}"
                )

    async def _call(self, block: Action, ctx: StepContext, inputs: Any) -> Any:
        """Call a block. Wait and call it again during flood control.

        A run that the user started by hand does not wait: the user is
        there, and is told at once.
        """
        waits = 0
        while True:
            try:
                return await block.run(ctx, inputs)
            except (RetryAfter, SurfaceBusyError) as e:
                if ctx.run.manual or waits >= self.FLOOD_MAX_WAITS:
                    raise
                waits += 1
                delay = min(e.retry_after, 3600) + random.uniform(
                    *self.FLOOD_WAKE_JITTER_RANGE_S
                )
                self._logger.warning(
                    f"Workflow {ctx.workflow_id}: {block.id} hit Telegram flood"
                    f" control; trying the step again in {delay:.0f}s."
                )
                await _sleep(delay)

    # -- by hand ----------------------------------------------------------

    async def run_by_hand(self, workflow_id: str, user: User) -> None:
        """Run a workflow because the user asked, and say how it went.

        This is what a workflow's command calls. Ask what the trigger wants
        to know first, then run the workflow for each event the trigger
        gives for the answer. The runs are not tried again if they fail.
        """
        bot = await UserInteractionApi.for_user(user)
        workflow = await self._store.get(workflow_id)
        if workflow is None or workflow.user_id != user.id:
            await bot.send_message("This workflow no longer exists.")
            return
        found = self.problems(workflow)
        if found:
            await bot.send_message(
                f"“{workflow.name}” cannot run. {found[0]}", parse_html=False
            )
            return
        trigger = self._trigger_of(workflow)
        if trigger.Manual is None:
            await bot.send_message(
                f"“{workflow.name}” cannot be started by hand.", parse_html=False
            )
            return
        answer = await self._ask(bot, workflow, trigger.Manual)
        if answer is None:
            return

        params = from_data(trigger.Params, workflow.trigger.params)
        trigger_ctx = self._trigger_context(workflow, user)
        try:
            events = await trigger.manual_events(trigger_ctx, params, answer)
        except ManualError as e:
            await bot.send_message(str(e), parse_html=False)
            return
        traces = []
        for event in events:
            trace = await self._run(workflow, user, event, manual=True, attempt=1)
            traces.append(trace)
            await trigger.manual_run_done(
                trigger_ctx, params, event, worked=trace["status"] != FAILED
            )
        await bot.send_message(self._summary(workflow, traces), parse_html=False)

    # -- trying a workflow out ----------------------------------------------

    async def test_events(self, workflow: Workflow) -> dict[str, Any]:
        """Return what a workflow can be tried out with.

        ``last`` is the event it last ran for, with the time of that run,
        or ``None`` if it has not run. ``now`` says whether its trigger can
        make an event without asking anything.
        """
        trigger = self._registry.get(workflow.trigger.block)
        return {
            "last": await self._store.last_event(workflow.id),
            "now": isinstance(trigger, Trigger) and trigger.Manual is NoQuestion,
        }

    async def run_test(
        self, workflow: Workflow, user: User, source: str
    ) -> dict[str, Any] | None:
        """Run a workflow once to try it out, and return the trace.

        ``workflow`` is run as it is given, and need not be the stored one:
        the editor tests what is not saved yet. ``source`` is ``last``, for
        the event the stored workflow last ran for, or ``now``, for an
        event that the trigger makes without asking. Return ``None`` if
        there is no such event.

        The steps do what they do: a test is a real run. It is not tried
        again if it fails, and it is kept in the history, marked as a test.
        """
        event: Any
        if source == "last":
            kept = await self._store.last_event(workflow.id)
            if kept is None:
                return None
            event = from_whole_data(kept["event"])
        else:
            trigger = self._registry.get(workflow.trigger.block)
            if not (isinstance(trigger, Trigger) and trigger.Manual is NoQuestion):
                return None
            params = from_data(trigger.Params, workflow.trigger.params)
            events = await trigger.manual_events(
                self._trigger_context(workflow, user), params, NoQuestion()
            )
            if not events:
                return None
            event = events[0]
        return await self._run(workflow, user, event, manual=True, attempt=1, test=True)

    @staticmethod
    async def _ask(
        bot: UserInteractionApi, workflow: Workflow, manual: type[Any]
    ) -> Any | None:
        """Ask the questions of a manual run, and return the answers.

        A question that depends on an earlier answer is left out unless
        the answer is the one it waits for. Return ``None`` if the user
        cancelled.
        """
        answers: dict[str, str] = {}
        for question in manual_questions(manual):
            if any(answers.get(k) != v for k, v in question.ask_if.items()):
                continue
            prompt = question.prompt or f"Run “{workflow.name}” for…"
            if question.choices is None:
                answers[question.name] = (await bot.request_user_input(prompt)).strip()
                continue
            choice = await bot.request_user_choice(
                prompt, [*question.choices, "Cancel"]
            )
            if not 0 <= choice < len(question.choices):
                return None
            answers[question.name] = question.choices[choice]
        return manual(**answers)

    @staticmethod
    def _summary(workflow: Workflow, traces: list[dict[str, Any]]) -> str:
        if not traces:
            return f"{workflow.name}: there was nothing to run."
        if len(traces) == 1:
            if traces[0]["status"] == STOPPED:
                # A block ended the run on purpose, so the run did what it
                # was asked to. Do not make that read like a failure.
                return f"{workflow.name}: done. The run ended at {_where(traces[0])}."
            return f"{workflow.name}: {outcome(traces[0])}"
        counts = {
            status: sum(1 for trace in traces if trace["status"] == status)
            for status in (FINISHED, STOPPED, FAILED)
        }
        return (
            f"{workflow.name}: {len(traces)} runs. {counts[FINISHED]} finished, "
            f"{counts[STOPPED]} ended early, {counts[FAILED]} failed."
        )
