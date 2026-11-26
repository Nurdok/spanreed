"""Run workflows: start their triggers, run their steps, keep the traces.

One event of a trigger is one run. The runs of a workflow go one at a time.
A run fills each step's inputs from the outputs before it, calls the step's
block, and stops at the first step that fails or that ends the run. A step
whose block is a loop runs the steps inside it once for each round. What
each step received and returned is kept as the run's trace.

A step can ask the user something. The run takes the user's turn to talk
at its first question, as a plugin does around a conversation, and keeps it
until it ends. If another conversation takes the turn, the run ends there,
without failing.

A run that fails is tried again for the same event, from its first step,
after a pause that doubles each time. So a step can run more than once for
one event, and a block must be safe to repeat. A workflow whose runs keep
failing, because a service is down for hours, is then tried only once every
half hour until a run works.

A run keeps its place after each of its steps (see ``_Place``). If Spanreed
is restarted in the middle of a run, the run goes on from the step it was
at: a question that waited for its answer is asked again.

The engine never writes a step's inputs or outputs to the log: they can hold
anything the user's services know. They are kept in the trace only.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import functools
import hashlib
import json
import logging
import random
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, cast

from telegram.error import RetryAfter

from spanreed.interaction import (
    PluginCommand,
    SurfaceBusyError,
    UserInteractionApi,
    UserInteractionPreempted,
    UserInteractionPriority,
)
from spanreed.interaction import commands
from spanreed.interaction import engine as turns
from spanreed.user import User
from spanreed.workflows import expressions, model
from spanreed.workflows.blocks import (
    Action,
    AskLoop,
    UntilLoop,
    BlockRegistry,
    Branch,
    Called,
    Interaction,
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
from spanreed.workflows.values import (
    NotPlain,
    from_data,
    from_whole_data,
    make_again,
    plain_data,
    to_data,
    whole_data,
)

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


# Why a run ended that lost its turn to talk, and one that got no answer.
TOOK_OVER = "Another conversation took over."

# How a step that asks waits for the user's turn, by its ``priority`` input.
_PRIORITIES = {
    "normal": UserInteractionPriority.NORMAL,
    "low": UserInteractionPriority.LOW,
    "high": UserInteractionPriority.HIGH,
}


class _InsideFailed(Exception):
    """A step inside a loop or a branch failed. The message says where and why."""


class _Turn:
    """What a run holds while it goes: its workflow's lock, and the turn to talk.

    The runs of a workflow go one at a time, so a run holds the lock of
    its workflow. A run that asks the user something also takes the user's
    turn to talk (``interaction.engine.user_interaction``) at its first
    step that asks, and keeps it until it ends, so that its questions are
    one conversation.

    A run that the user started by hand has the turn already: the flow of
    its command took it. Such a run would wait for the lock while a run in
    the background, which holds the lock, waits for the turn. So a run
    gives the lock up while it waits for the turn, and takes it back
    after. A run by hand can go in between.
    """

    def __init__(self, user_id: int, lock: asyncio.Lock) -> None:
        self._user_id = user_id
        self._lock = lock
        self._locked = False
        self._held: contextlib.AbstractAsyncContextManager[None] | None = None

    async def start(self) -> None:
        """Wait for the workflow's other runs to be over."""
        await self._lock.acquire()
        self._locked = True

    async def take(self, priority: UserInteractionPriority) -> None:
        """Wait for the user's turn to talk, unless the run has it."""
        if self._held is not None or turns.holds_turn(self._user_id):
            return
        self._lock.release()
        self._locked = False
        turn = turns.user_interaction(self._user_id, priority=priority)
        await turn.__aenter__()
        self._held = turn
        await self._lock.acquire()
        self._locked = True

    async def lost(self) -> bool:
        """Return whether the run was cancelled to give its turn away.

        Call this while handling the ``CancelledError``. The turn is given
        up either way. ``False`` means the run itself is to end: its
        workflow was turned off, or Spanreed stops.
        """
        if self._held is None:
            return False
        turn, self._held = self._held, None
        try:
            await turn.__aexit__(*sys.exc_info())
        except UserInteractionPreempted:
            return True
        return False

    async def end(self) -> None:
        """Give up the turn to talk, if the run has it, and the lock."""
        turn, self._held = self._held, None
        try:
            if turn is not None:
                await turn.__aexit__(None, None, None)
        finally:
            if self._locked:
                self._locked = False
                self._lock.release()


def digest(workflow: Workflow) -> str:
    """Return a short text that differs when the trigger or the steps do."""
    data = workflow.to_data()
    parts = json.dumps([data["trigger"], data["steps"]], sort_keys=True)
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass
class _Again:
    """What a run that a restart cut short goes on with."""

    id: str
    started_at: str
    event: Any
    # The steps that are done, as the trace had them, and what each gave.
    entries: list[dict[str, Any]]
    outputs: dict[str, Any]
    talked: bool
    # How many times the run was picked up before.
    picked_up: int


class _Place:
    """How far a run got, kept so that it can go on after a restart.

    The place is kept when the run starts and after each of its steps: the
    event, the steps that are done as the trace has them, and what each of
    them gave. It is dropped when the run is over. A run that finds its
    place after a restart starts at its first step that is not done. A
    restart in the middle of a step starts the step over, and a step that
    holds steps is one step.

    What a step gave is kept only if it can be made again as it was (see
    ``values.plain_data``). From a step that gave more than that, bytes
    for example, the place is the start of the run: the steps that go on
    after a restart must not work with something else than they would
    have. The same goes for the event of a run by hand. The event of
    another run comes from its trigger again.

    A place that would be larger than ``MAX_SIZE`` is not kept at all.
    """

    # In characters of JSON. An event can hold a day of health records.
    MAX_SIZE = 200_000

    def __init__(
        self,
        store: WorkflowStore,
        workflow: Workflow,
        trace: dict[str, Any],
        event: Any,
        event_class: type[Any],
        output_class: Callable[[Step], type[Any]],
    ) -> None:
        """Create the place of the run with this trace, for this event.

        ``event_class`` is the class of the trigger's events, and
        ``output_class`` gives the class of what a step gives.
        """
        self._store = store
        self._workflow = workflow
        self._trace = trace
        self._output_class = output_class
        self._head: dict[str, Any] = {
            "workflow": digest(workflow),
            "manual": trace["manual"],
            "started_at": trace["started_at"],
            "event": whole_data(event),
        }
        try:
            # What is plain is whole as it is: the event is kept once.
            self._head["event"] = plain_data(event, event_class)
            self._head["event_is_plain"] = True
        except NotPlain:
            self._head["event_is_plain"] = False
        # What each step that is done gave, while all of it is plain.
        self._outputs: dict[str, Any] | None = {}
        self._logger = logging.getLogger("spanreed.workflows.engine")

    async def keep(self, context: dict[str, Any], talked: bool) -> None:
        """Keep the place of the run as it is now."""
        entries = self._trace["steps"]
        if self._outputs is not None:
            try:
                for step in self._workflow.steps[len(self._outputs) : len(entries)]:
                    self._outputs[step.id] = plain_data(
                        context[step.id], self._output_class(step)
                    )
            except NotPlain:
                self._outputs = None
        place = {
            **self._head,
            "picked_up": self._trace.get("picked_up", 0),
            "talked": talked,
            "steps": entries if self._outputs is not None else [],
            "outputs": self._outputs or {},
        }
        try:
            if len(json.dumps(place)) > self.MAX_SIZE:
                # Not worth writing after every step. The run starts over
                # after a restart, if its trigger gives the event again.
                await self._store.drop_place(self._workflow.id, self._trace["id"])
                return
            await self._store.keep_place(self._workflow.id, self._trace["id"], place)
        except Exception:
            # The run goes on. It starts over after a restart, as it did
            # before places were kept.
            self._logger.exception(
                f"Could not keep the place of run {self._trace['id']}"
                f" of workflow {self._workflow.id}."
            )

    async def drop(self) -> bool:
        """Drop the place: the run is over. Return whether that worked."""
        try:
            await self._store.drop_place(self._workflow.id, self._trace["id"])
        except Exception:
            self._logger.exception(
                f"Could not drop the place of run {self._trace['id']}"
                f" of workflow {self._workflow.id}."
            )
            return False
        return True


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def outcome(trace: dict[str, Any]) -> str:
    """Return how a run ended, as the end of a sentence about it."""
    status = trace["status"]
    if status == FINISHED:
        return "finished."
    if status == CANCELLED:
        return "was cancelled."
    if status == STOPPED:
        return f"stopped at {_where(trace)}."
    return f"failed at {_where(trace)}: {trace['steps'][-1]['error']}"


def _where(trace: dict[str, Any]) -> str:
    """Return the last step that a run got to: its number and its title."""
    if not trace["steps"]:
        return "its start"
    return f"step {len(trace['steps'])}, {trace['steps'][-1]['title']}"


class _Caller:
    """The user's other workflows, for the steps of one run. See ``Workflows``."""

    def __init__(
        self,
        engine: WorkflowEngine,
        user: User,
        manual: bool,
        chain: tuple[str, ...],
        interaction: Interaction,
    ) -> None:
        self._engine = engine
        self._user = user
        self._manual = manual
        # The workflows that are running on the way to this run, this one
        # last. None of them can be run again from here.
        self._chain = chain
        self._interaction = interaction

    async def _find(self, name: str) -> Workflow | None:
        """Return the user's workflow of this name, if there is exactly one."""
        workflows = await self._engine.store.list_for_user(self._user.id)
        for same in (
            [w for w in workflows if w.name == name],
            [w for w in workflows if w.name.lower() == name.lower()],
        ):
            if len(same) == 1:
                return same[0]
        return None

    def _why_not(self, name: str, workflow: Workflow | None) -> str | None:
        """Return why a workflow cannot be run from here, if it cannot."""
        if workflow is None:
            return f"There is not one workflow called “{name}”."
        if workflow.id in self._chain:
            return f"“{workflow.name}” is running: it cannot run itself."
        if not workflow.enabled:
            return f"“{workflow.name}” is off."
        found = self._engine.problems(workflow)
        if found:
            return f"“{workflow.name}” cannot run. {found[0]}"
        trigger = self._engine.registry.get(workflow.trigger.block)
        if not (isinstance(trigger, Trigger) and trigger.Manual is NoQuestion):
            return (
                f"“{workflow.name}” cannot be run by another workflow: its "
                "trigger asks before it starts, or cannot be started by hand."
            )
        return None

    async def on_offer(self, names: Iterable[str]) -> list[str]:
        """Return which of these names are workflows that ``run`` can run now."""
        return [
            name
            for name in names
            if self._why_not(name, await self._find(name)) is None
        ]

    async def run(self, name: str) -> Called:
        """Run the user's workflow of this name, and return how it went."""
        workflow = await self._find(name)
        why_not = self._why_not(name, workflow)
        if workflow is None or why_not is not None:
            raise ValueError(why_not)
        trace = await self._engine.run_called(
            workflow, self._user, manual=self._manual, chain=self._chain
        )
        if trace.get("talked"):
            self._interaction.note_talk()
        if trace["status"] == FAILED:
            raise RuntimeError(f"“{workflow.name}” {outcome(trace)}")
        if trace.get("conversation_over"):
            # The conversation that both runs were in is gone.
            raise StopRun(trace["steps"][-1]["reason"], conversation_over=True)
        return Called(
            name=workflow.name, status=trace["status"], outcome=outcome(trace)
        )


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
    # stopped together: a burst of them in the second that a ban lifts is
    # how the ban comes back.
    FLOOD_MAX_WAITS = 3
    FLOOD_WAKE_JITTER_RANGE_S = (60.0, 600.0)
    # The trace of a loop keeps this many of its rounds: the first ones,
    # and one that failed.
    ROUNDS_KEPT = 50
    # The seconds in one of the minutes that a step waits for an answer.
    # Tests make it shorter.
    GIVE_UP_UNIT_S = 60.0
    # How long a step that runs another workflow waits while that one is
    # in a run of its own, before the step fails.
    CALL_WAIT_S = 60.0

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
        # What each run in progress holds, by the id of the run.
        self._turns: dict[str, _Turn] = {}
        # Workflows whose user was told that they fail, until a run works,
        # each with the time of its last failed try.
        self._failing: dict[str, float] = {}
        # The runs by hand that go on after a restart.
        self._going_on: set[asyncio.Task[None]] = set()
        # Whether the engine stops: a run that ends for that keeps its place.
        self._stopping = False
        # The runs that are over and whose place could not be dropped.
        # Nothing goes on from those.
        self._over: set[str] = set()
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
        """Start the stored workflows of these users.

        A run that the last process did not get to the end of goes on. One
        that its trigger started goes on when the trigger gives its event
        again. One that the user started by hand goes on now.
        """
        for user_id in user_ids:
            for workflow in await self._store.list_for_user(user_id):
                self._apply(workflow)
                await self._go_on_by_hand(workflow)

    async def stop(self) -> None:
        """Stop every trigger, and any run that is in progress.

        The runs keep their places, to go on when Spanreed is back.
        """
        self._stopping = True
        tasks = [*self._sources.values(), *self._going_on]
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

    async def change_trigger(self, workflow: Workflow) -> None:
        """Store a workflow that got another trigger, and start that one.

        What the old trigger kept would not fit the new one: where it had
        got to, the events that still waited for a run, and the last
        event. All of it is dropped. The history and what the steps
        remember stay.
        """
        self._stop_source(workflow.id)
        self._failing.pop(workflow.id, None)
        await self._store.forget_trigger(workflow.id)
        await self.save(workflow)

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

    def asks(self, workflow: Workflow) -> bool:
        """Return whether a step of the workflow asks the user something."""

        def any_asks(steps: list[Step]) -> bool:
            return any(
                getattr(self._registry.get(step.block), "converses", False)
                or any_asks(step.steps)
                or any_asks(step.otherwise)
                for step in steps
            )

        return any_asks(workflow.steps)

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
        chain: tuple[str, ...] = (),
        called_by: str | None = None,
        kept: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the steps of a workflow for one event, and return the trace.

        ``test`` marks a run that the editor made to try a workflow out.
        ``chain`` holds the workflows whose steps had this one run, first
        one first, and ``called_by`` is the name of the last of them.
        ``kept`` is the place of a run by hand that goes on after a
        restart. A run that a trigger started looks for its place itself.
        """
        # A test is not worth going on with, and a run inside the step of
        # another run starts over with that step.
        keeps = not test and called_by is None
        if keeps and not manual:
            kept = await self._cut_short(workflow, event)
        again = self._made_again(workflow, kept, event) if kept else None
        if again is not None:
            event = again.event
        run = RunInfo(id=again.id if again else secrets.token_hex(6), manual=manual)
        interaction = Interaction(user)
        ctx = StepContext(
            user=user,
            workflow_id=workflow.id,
            run=run,
            interaction=interaction,
            memory=self._store.memory(workflow.id),
            workflows=_Caller(self, user, manual, (*chain, workflow.id), interaction),
        )
        context: dict[str, Any] = {model.TRIGGER_NAME: event, model.RUN_NAME: run}
        trace: dict[str, Any] = {
            "id": run.id,
            "workflow_id": workflow.id,
            "started_at": again.started_at if again else _now(),
            "manual": manual,
            "attempt": attempt,
            "trigger": {
                "block": workflow.trigger.block,
                "outputs": to_data(event),
            },
            "steps": [],
            "status": FINISHED,
        }
        if called_by is not None:
            trace["called_by"] = called_by
        if test:
            trace["test"] = True
        elif again is None:
            await self._keep_event(workflow, trace["started_at"], event)
        if again is not None:
            # The steps that are done, as they were, and what they gave.
            trace["steps"] = list(again.entries)
            trace["picked_up"] = again.picked_up + 1
            context.update(again.outputs)
            if again.talked:
                interaction.note_talk()
        place = None
        if keeps:
            place = _Place(
                self._store,
                workflow,
                trace,
                event,
                self._trigger_of(workflow).Event,
                lambda step: cast(Action, self._registry.get(step.block)).Output,
            )
        turn = _Turn(user.id, self._locks.setdefault(workflow.id, asyncio.Lock()))
        self._turns[run.id] = turn
        try:
            if called_by is None:
                await turn.start()
            else:
                # The step that waits for this run holds its own workflow
                # in the meantime. Two workflows that run each other must
                # not wait for each other for good.
                async with asyncio.timeout(self.CALL_WAIT_S):
                    await turn.start()
            try:

                async def keep_place() -> None:
                    if place is not None:
                        await place.keep(context, ctx.interaction.talked)

                # Also before the first step: that one may be a question.
                await keep_place()
                trace["status"] = await self._run_steps(
                    workflow.steps[len(trace["steps"]) :],
                    ctx,
                    context,
                    trace["steps"],
                    keep_place,
                )
            except asyncio.CancelledError:
                # A step turns the loss of the turn into its own end, so
                # this is the run being cancelled, unless the turn was lost
                # between two steps.
                if not await turn.lost():
                    trace["status"] = CANCELLED
                    raise
                trace["status"] = STOPPED
                trace["conversation_over"] = True
            finally:
                if trace["steps"] and trace["steps"][-1].get("conversation_over"):
                    trace["conversation_over"] = True
                trace["finished_at"] = _now()
                if ctx.interaction.talked:
                    trace["talked"] = True
                if (
                    place is not None
                    and trace["status"] == CANCELLED
                    and self._stopping
                ):
                    # Spanreed stops. The run is not over: it keeps its
                    # place, and has no trace yet.
                    pass
                else:
                    await self._keep(workflow, trace)
                    if place is not None and not await place.drop():
                        self._over.add(run.id)
        finally:
            del self._turns[run.id]
            await turn.end()
        return trace

    # -- going on after a restart -------------------------------------------

    async def _cut_short(self, workflow: Workflow, event: Any) -> dict[str, Any] | None:
        """Return the place of the run that a restart cut short for an event.

        The run is one that the trigger started. There is at most one, and
        it is the one for this event if the trigger says so. A place that
        is for another event, or for the workflow as it was before a
        change, is dropped: nothing will go on from it.
        """
        try:
            places = await self._store.places(workflow.id)
        except Exception:
            self._logger.exception(
                f"Could not read the places of the runs of workflow {workflow.id}."
            )
            return None
        found = None
        for run_id, place in places.items():
            if place.get("manual") or run_id in self._turns:
                continue
            if run_id in self._over:
                # Its run ended, and its place is still there.
                await self._store.drop_place(workflow.id, run_id)
                self._over.discard(run_id)
                continue
            if (
                found is None
                and place.get("workflow") == digest(workflow)
                and self._same_event(workflow, place, event)
            ):
                found = {**place, "id": run_id}
            else:
                await self._store.drop_place(workflow.id, run_id)
        return found

    def _same_event(
        self, workflow: Workflow, place: dict[str, Any], event: Any
    ) -> bool:
        kept = place.get("event")
        if not isinstance(kept, dict):
            return False
        try:
            return self._trigger_of(workflow).same_event(kept, event)
        except Exception:
            self._logger.exception(
                f"The trigger of workflow {workflow.id} could not tell whether"
                " an event is the one a run was cut short for."
            )
            return False

    def _made_again(
        self, workflow: Workflow, place: dict[str, Any], event: Any
    ) -> _Again | None:
        """Return what a run goes on with, from the place that was kept.

        ``event`` is what the trigger gave now, or ``None`` for a run by
        hand: the event that was kept is used if it can be made again as
        it was. Return ``None`` if there is no event to go on with.

        The run starts at its first step if what the steps gave cannot be
        made again.
        """
        if place.get("workflow") != digest(workflow):
            return None
        try:
            if not place.get("event_is_plain"):
                raise NotPlain("the event")
            event = make_again(self._trigger_of(workflow).Event, place.get("event"))
        except NotPlain:
            if event is None:
                return None
        entries = place.get("steps")
        given = place.get("outputs")
        outputs: dict[str, Any] = {}
        try:
            if not isinstance(entries, list) or not isinstance(given, dict):
                raise NotPlain("no steps")
            if len(entries) > len(workflow.steps):
                raise NotPlain("more steps than the workflow has")
            for step, entry in zip(workflow.steps, entries):
                if entry.get("id") != step.id or entry.get("status") != OK:
                    raise NotPlain("a step that is not done")
                block = cast(Action, self._registry.get(step.block))
                outputs[step.id] = make_again(block.Output, given.get(step.id))
        except (NotPlain, AttributeError):
            entries, outputs = [], {}
        picked_up = place.get("picked_up")
        return _Again(
            id=str(place["id"]),
            started_at=str(place.get("started_at") or _now()),
            event=event,
            entries=entries,
            outputs=outputs,
            talked=bool(place.get("talked")),
            picked_up=picked_up if isinstance(picked_up, int) else 0,
        )

    async def _go_on_by_hand(self, workflow: Workflow) -> None:
        """Go on with the runs of a workflow that the user started by hand.

        Each goes on in a task of its own. A run whose event cannot be
        made again, or whose workflow changed or cannot run, is forgotten.
        """
        try:
            places = await self._store.places(workflow.id)
        except Exception:
            self._logger.exception(
                f"Could not read the places of the runs of workflow {workflow.id}."
            )
            return
        for run_id, place in places.items():
            if not place.get("manual"):
                continue
            kept = {**place, "id": run_id}
            if self.problems(workflow) or not self._made_again(workflow, kept, None):
                await self._store.drop_place(workflow.id, run_id)
                continue
            task = asyncio.create_task(
                self._gone_on_by_hand(workflow, kept),
                name=f"workflow-{workflow.id}-run-{run_id}",
            )
            self._going_on.add(task)
            task.add_done_callback(self._going_on.discard)

    async def _gone_on_by_hand(self, workflow: Workflow, kept: dict[str, Any]) -> None:
        """Run the rest of a run by hand, and end it as ``run_by_hand`` does."""
        try:
            again = self._made_again(workflow, kept, None)
            if again is None:
                return
            user = await User.find_by_id(workflow.user_id)
            trace = await self._run(
                workflow, user, again.event, manual=True, attempt=1, kept=kept
            )
            trigger = self._trigger_of(workflow)
            await trigger.manual_run_done(
                self._trigger_context(workflow, user),
                from_data(trigger.Params, workflow.trigger.params),
                again.event,
                worked=trace["status"] != FAILED,
            )
            if trace.get("talked") and trace["status"] != FAILED:
                return
            bot = await UserInteractionApi.for_user(user)
            await bot.send_message(self._summary(workflow, [trace]), parse_html=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                f"Could not go on with run {kept['id']} of workflow {workflow.id}."
            )

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
        after_each: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """Run steps in order, up to one that fails or ends the run.

        Add the entry of each step that ran to ``entries``, as it ends, so
        that a run that is cancelled keeps the steps it got through. Return
        how the steps ended: ``FINISHED``, or the status of the last one.

        ``after_each`` is awaited after each step that the next one
        follows.
        """
        for step in steps:
            entry = await self._run_step(step, ctx, context)
            entries.append(entry)
            if entry["status"] != OK:
                return str(entry["status"])
            if after_each is not None:
                await after_each()
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
            written = dict(step.inputs)
            if isinstance(block, UntilLoop):
                # Filled in after each round, with what its steps gave.
                written[block.late_input] = False
            inputs = from_data(
                block.Input, expressions.render(written, context), written
            )
            entry["inputs"] = to_data(inputs)
            if isinstance(block, UntilLoop):
                entry["inputs"][block.late_input] = step.inputs.get(block.late_input)
            said: list[dict[str, str]] = []
            try:
                if isinstance(block, AskLoop):
                    outputs = await self._run_asked_rounds(
                        step, block, inputs, ctx, context, entry, said
                    )
                elif isinstance(block, UntilLoop):
                    outputs = await self._run_rounds_until(
                        step, block, inputs, ctx, context, entry
                    )
                else:
                    if isinstance(block, Loop):
                        await self._run_rounds(step, block, inputs, ctx, context, entry)
                    self._speaks(ctx, inputs, said)

                    def run() -> Awaitable[Any]:
                        return block.run(ctx, inputs)

                    outputs = await self._call(block, ctx, run)
            finally:
                # What a step that only sends has sent is in its inputs.
                if said and block.converses:
                    entry["conversation"] = said
            context[step.id] = outputs
            entry["outputs"] = to_data(outputs)
            if isinstance(block, Branch):
                await self._run_side(step, block, inputs, ctx, context, entry)
        except StopRun as stop:
            entry["status"] = STOPPED
            entry["reason"] = stop.reason
            if stop.whole_run:
                entry["ends_run"] = True
            if stop.conversation_over:
                entry["conversation_over"] = True
        except asyncio.CancelledError:
            if not await self._turns[ctx.run.id].lost():
                raise
            # Another conversation took the user's turn to talk. That is
            # no failure: end the run here, as a plugin drops a question
            # that is preempted.
            entry["status"] = STOPPED
            entry["reason"] = TOOK_OVER
            entry["ends_run"] = True
            entry["conversation_over"] = True
        except _InsideFailed as failed:
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
        ``_InsideFailed`` at the first step that fails.

        Add the rounds to the loop's ``entry`` of the trace.
        """
        values = loop.rounds(inputs)
        entry["round_name"] = loop.round_name
        entry["round_count"] = len(values)
        entry["rounds_run"] = 0
        entry["rounds"] = []
        for number, value in enumerate(values, start=1):
            entry["rounds_run"] = number
            await self._run_round(
                step, loop, value, f"{number} of {len(values)}", ctx, context, entry
            )

    async def _run_asked_rounds(
        self,
        step: Step,
        loop: AskLoop,
        inputs: Any,
        ctx: StepContext,
        context: dict[str, Any],
        entry: dict[str, Any],
        said: list[dict[str, str]],
    ) -> Any:
        """Run the steps inside a loop once for each answer the user gives.

        The loop asks for a round, the steps inside run for the answer,
        and the loop asks again, until it has no more to ask.

        Add the rounds to the loop's ``entry`` of the trace, keep what the
        loop asked in ``said``, and return what the steps after it see.
        """
        values: list[Any] = []
        entry["round_name"] = loop.round_name
        entry["round_count"] = 0
        entry["rounds_run"] = 0
        entry["rounds"] = []

        def ask() -> Awaitable[Any]:
            return loop.ask(ctx, inputs, values)

        while True:
            # The steps of the round before spoke for themselves.
            self._speaks(ctx, inputs, said)
            value = await self._call(loop, ctx, ask)
            if value is None:
                return loop.after(inputs, values)
            values.append(value)
            # How many there will be is not known: count them as they come.
            entry["round_count"] = entry["rounds_run"] = len(values)
            await self._run_round(
                step, loop, value, str(len(values)), ctx, context, entry
            )

    async def _run_rounds_until(
        self,
        step: Step,
        loop: UntilLoop,
        inputs: Any,
        ctx: StepContext,
        context: dict[str, Any],
        entry: dict[str, Any],
    ) -> Any:
        """Run the steps inside a loop until its condition holds.

        The condition is filled in after each round, with what the steps
        of the round gave. A step of the round that did not run gives
        nothing, and what is asked of nothing does not hold.

        Add the rounds to the loop's ``entry`` of the trace, and return
        what the steps after the loop see.
        """
        entry["round_name"] = loop.round_name
        entry["round_count"] = 0
        entry["rounds_run"] = 0
        entry["rounds"] = []
        condition = step.inputs.get(loop.late_input, False)
        number = 0
        for number in range(1, loop.limit(inputs) + 1):
            entry["round_count"] = entry["rounds_run"] = number
            seen = {**context, step.id: loop.round(inputs, number)}
            await self._run_round(
                step, loop, seen[step.id], str(number), ctx, context, entry, seen
            )
            for inside in step.steps:
                seen.setdefault(inside.id, expressions.NOTHING)
            try:
                stop = expressions.render(condition, seen)
            except expressions.ExpressionError as e:
                if not str(e).endswith(expressions.NO_SUCH_VALUE):
                    raise
                stop = False
            if isinstance(stop, str):
                stop = stop.strip().lower() in ("true", "yes")
            if stop:
                return loop.after(inputs, number, True)
        return loop.after(inputs, number, False)

    async def _run_round(
        self,
        step: Step,
        loop: Loop,
        value: Any,
        which: str,
        ctx: StepContext,
        context: dict[str, Any],
        entry: dict[str, Any],
        seen: dict[str, Any] | None = None,
    ) -> None:
        """Run the steps inside a loop for one round, and add it to the trace.

        ``which`` names the round in a failure: "3 of 5". Raise ``StopRun``
        if a step inside ends the whole run, and ``_InsideFailed`` if one
        fails. ``seen`` is what the steps inside see, if the caller is to
        see what they gave.
        """
        number = entry["rounds_run"]
        steps: list[dict[str, Any]] = []
        if seen is None:
            seen = {**context, step.id: value}
        status = await self._run_steps(step.steps, ctx, seen, steps)
        kept = {"number": number, "status": status, "steps": steps}
        ends_run = status == STOPPED and bool(steps[-1].get("ends_run"))
        if len(entry["rounds"]) < self.ROUNDS_KEPT or status == FAILED or ends_run:
            entry["rounds"].append(kept)
        if ends_run:
            raise StopRun(
                steps[-1].get("reason") or "A step ended the run.",
                whole_run=True,
                conversation_over=bool(steps[-1].get("conversation_over")),
            )
        if status == FAILED:
            failed = steps[-1]
            raise _InsideFailed(
                f"{loop.round_name} {which}, {failed['title']}: {failed['error']}"
            )

    async def _run_side(
        self,
        step: Step,
        branch: Branch,
        inputs: Any,
        ctx: StepContext,
        context: dict[str, Any],
        entry: dict[str, Any],
    ) -> None:
        """Run the side of a branch that its inputs choose.

        The steps inside see what the steps before the branch gave, the
        branch itself, and each other. What they give is not there after
        the branch. Raise ``_InsideFailed`` if a step inside fails, and
        ``StopRun`` if one ends the run.

        Add which side was taken, and its steps, to the branch's ``entry``
        of the trace.
        """
        first = branch.takes_first(inputs)
        entry["took"] = "steps" if first else "otherwise"
        entry["took_title"] = branch.first_title if first else branch.second_title
        entry["inside"] = []
        status = await self._run_steps(
            step.steps if first else step.otherwise, ctx, dict(context), entry["inside"]
        )
        if status == FAILED:
            failed = entry["inside"][-1]
            raise _InsideFailed(f"{failed['title']}: {failed['error']}")
        if status == STOPPED:
            last = entry["inside"][-1]
            raise StopRun(
                last.get("reason") or "A step ended the run.",
                whole_run=bool(last.get("ends_run")),
                conversation_over=bool(last.get("conversation_over")),
            )

    def _speaks(
        self, ctx: StepContext, inputs: Any, said: list[dict[str, str]]
    ) -> None:
        """Make the run's conversation that of the step with these inputs.

        What the step asks and says from now on is kept in ``said``. A
        question of the step takes the user's turn to talk first, unless
        the run has it, with the step's ``priority``. And it gets the
        minutes of the step's ``give_up_after`` for its answer, if the
        step sets any: the run ends if that is not enough.
        """
        priority = _PRIORITIES.get(
            getattr(inputs, "priority", ""), UserInteractionPriority.NORMAL
        )
        turn = self._turns[ctx.run.id]
        minutes = getattr(inputs, "give_up_after", 0)
        waits = isinstance(minutes, int) and minutes > 0
        ctx.interaction.keep_in(said)
        ctx.interaction.asks_with(
            lambda: turn.take(priority),
            minutes * self.GIVE_UP_UNIT_S if waits else None,
            f"There was no answer for {minutes} "
            f"{'minute' if minutes == 1 else 'minutes'}.",
        )

    async def _call(
        self, block: Action, ctx: StepContext, call: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Call a block, through ``call``. Wait and call again during flood control.

        A run that the user started by hand does not wait: the user is
        there, and is told at once.
        """
        waits = 0
        while True:
            try:
                return await call()
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

    # -- from a step of another workflow -----------------------------------

    async def run_called(
        self, workflow: Workflow, user: User, *, manual: bool, chain: tuple[str, ...]
    ) -> dict[str, Any]:
        """Run a workflow because a step of another one asked, and return the trace.

        The run is for an event that the trigger makes without asking, as
        for a run by hand. It happens in the task of the step, so in its
        conversation, and it is not tried again if it fails: the step
        fails, and its own run is.
        """
        trigger = self._trigger_of(workflow)
        params = from_data(trigger.Params, workflow.trigger.params)
        events = await trigger.manual_events(
            self._trigger_context(workflow, user), params, NoQuestion()
        )
        if not events:
            raise ValueError(f"“{workflow.name}” has nothing to run for.")
        caller = await self._store.get(chain[-1]) if chain else None
        try:
            return await self._run(
                workflow,
                user,
                events[0],
                manual=manual,
                attempt=1,
                chain=chain,
                called_by=caller.name if caller is not None else "another workflow",
            )
        except TimeoutError:
            raise RuntimeError(
                f"“{workflow.name}” is in a run of its own, and still was after "
                f"{self.CALL_WAIT_S:.0f} seconds."
            ) from None

    # -- by hand ----------------------------------------------------------

    async def run_by_hand(self, workflow_id: str, user: User) -> None:
        """Run a workflow because the user asked, and say how it went.

        This is what a workflow's command calls. Ask what the trigger wants
        to know first, then run the workflow for each event the trigger
        gives for the answer. The runs are not tried again if they fail.

        One run that asked or told the user something, and did not fail,
        is not summed up: it spoke for itself.
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
        if (
            len(traces) == 1
            and traces[0].get("talked")
            and traces[0]["status"] != FAILED
        ):
            # The run asked or told the user something, and ended as it was
            # meant to. There is nothing to add to that.
            return
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
            "asks": self.asks(workflow),
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

        A workflow that asks the user something is not run here: a test is
        not a conversation. Raise ``ValueError``; see ``asks``.
        """
        if self.asks(workflow):
            raise ValueError(
                "A workflow that asks you something cannot be tested here. "
                "Run it by hand."
            )
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
