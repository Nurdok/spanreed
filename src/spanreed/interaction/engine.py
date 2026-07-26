"""Surface-neutral user-interaction engine.

This is the serialization core extracted from ``TelegramBotApi``: per-user
interaction queues with priorities and preemption, plus the pending-prompt
bookkeeping that lets any surface deliver a user's answer. Everything here is
keyed by the *Spanreed* user id (never a Telegram chat id) and has no
dependency on any surface or on the Telegram application lifecycle.

State is process-local (module-level dicts): Spanreed is a single asyncio
process, and this state was previously process-local too (in the Telegram
application's ``bot_data``). Pending prompts do not survive a restart; a
late answer to a pre-restart prompt gets ``PromptResolution.UNKNOWN`` and
surfaces render it as "expired".
"""

import asyncio
import collections
import contextlib
import datetime
import enum
import logging
import uuid
from collections.abc import AsyncGenerator, Coroutine
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

from spanreed.interaction.errors import UserInteractionPreempted

_logger = logging.getLogger(__name__)

# How long a resolved prompt id is remembered, so a late answer can be told
# "already answered" instead of "expired".
_TOMBSTONE_MAX = 500


class UserInteractionPriority(IntEnum):
    HIGH = enum.auto()
    NORMAL = enum.auto()
    LOW = enum.auto()


class UserInteraction:
    def __init__(
        self,
        user_id: int,
        priority: UserInteractionPriority,
    ) -> None:
        self.user_id = user_id
        self.priority = priority
        self.event = asyncio.Event()
        # This is the timeout for the actual interaction, not for waiting in
        # the queue. It is enforced if there's another interaction waiting,
        # regardless of priority.
        self.timeout = datetime.timedelta(hours=1)
        self.start_time: Optional[datetime.datetime] = None
        task: asyncio.Task | None = asyncio.current_task()
        if task is None:
            raise RuntimeError("No current task")
        self.task: asyncio.Task = task
        self.preempted = False
        self.running = False

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        coro = self.task.get_coro()
        task_name = coro.cr_code.co_qualname if coro is not None else "unknown"
        return f"UserInteraction<{task_name} @ {self.priority.name}>"

    def allow_to_run(self) -> None:
        self.event.set()

    async def wait_to_run(self) -> None:
        await self.event.wait()
        self.running = True
        self.start_time = datetime.datetime.now()

    def preempt(self) -> None:
        self.preempted = True
        self.task.cancel()


UserInteractionQueues = dict[UserInteractionPriority, list[UserInteraction]]

_locks: dict[int, asyncio.Lock] = {}
_queues: dict[int, UserInteractionQueues] = {}
_current: dict[int, UserInteraction | None] = {}


def reset_state_for_tests() -> None:
    _locks.clear()
    _queues.clear()
    _current.clear()
    _pending_prompts.clear()
    _current_input_prompt.clear()
    _tombstones.clear()
    _user_flow_tasks.clear()


# ---------------------------------------------------------------------------
# User-initiated flows (/do, palette commands)
# ---------------------------------------------------------------------------

# One user-initiated flow at a time per user, newest wins — shared across
# surfaces (design §2.6): a palette invocation cancels an in-progress /do
# flow and vice versa. Without cross-surface cancel, a HIGH palette command
# would queue forever behind an abandoned open /do menu (HIGH does not
# preempt HIGH).
_user_flow_tasks: dict[int, asyncio.Task] = {}


def start_user_flow(user_id: int, coro: Coroutine) -> asyncio.Task:
    """Start a user-initiated flow, cancelling the user's previous one."""
    previous = _user_flow_tasks.get(user_id)
    if previous is not None and not previous.done():
        _logger.info(f"Cancelling previous user flow for user {user_id}")
        previous.cancel()
    task = asyncio.create_task(coro)
    _user_flow_tasks[user_id] = task

    def _cleanup(_: asyncio.Task) -> None:
        if _user_flow_tasks.get(user_id) is task:
            del _user_flow_tasks[user_id]

    task.add_done_callback(_cleanup)
    return task


def get_user_interaction_lock(user_id: int) -> asyncio.Lock:
    return _locks.setdefault(user_id, asyncio.Lock())


def _get_user_interaction_queues(user_id: int) -> UserInteractionQueues:
    return _queues.setdefault(user_id, {prio: [] for prio in UserInteractionPriority})


def has_active_user_interaction(user_id: int) -> bool:
    """Whether a user interaction is currently in progress for this user.

    Lets background flows (e.g. the Obsidian progress bar) avoid sending
    unsolicited messages when the user isn't already in a conversation.
    """
    return _current.get(user_id) is not None


def get_current_user_interaction(user_id: int) -> UserInteraction | None:
    return _current.setdefault(user_id, None)


def _set_current_user_interaction(
    user_id: int, user_interaction: UserInteraction
) -> None:
    if _current.setdefault(user_id, None) is not None:
        raise RuntimeError("User interaction already set")
    _current[user_id] = user_interaction


def _clear_current_user_interaction(user_id: int) -> None:
    _logger.info(f"Clearing current user interaction {_current.get(user_id)=}")
    _current[user_id] = None


async def _try_to_allow_next_user_interaction(
    user_id: int, delay: datetime.timedelta | None = None
) -> None:
    if delay is not None:
        await asyncio.sleep(delay.total_seconds())
    _logger.info("Trying to allow next user interaction")
    _preempt_user_interaction_if_needed(user_id)
    if get_current_user_interaction(user_id) is not None:
        return

    interaction_queue: UserInteractionQueues = _get_user_interaction_queues(user_id)

    for priority in UserInteractionPriority:  # high to low
        if interaction_queue[priority]:
            user_interaction: UserInteraction = interaction_queue[priority].pop(0)
            _logger.info(f"Allowing {user_interaction=}")
            user_interaction.allow_to_run()
            _set_current_user_interaction(user_id, user_interaction)
            return

    _logger.info("No user interaction to allow")


def _preempt_user_interaction_if_needed(user_id: int) -> None:
    _logger.info("Checking if we need to preempt user interaction")
    current_interaction: UserInteraction | None = get_current_user_interaction(user_id)
    if current_interaction is None:
        _logger.info("No current user interaction, so not preempting")
        return

    current_interaction_timed_out = (
        current_interaction.start_time is not None
        and (datetime.datetime.now() - current_interaction.start_time).total_seconds()
        > current_interaction.timeout.total_seconds()
    )

    user_interaction_queues = _get_user_interaction_queues(user_id)
    for possible_priority in UserInteractionPriority:  # high to low
        if (
            possible_priority >= current_interaction.priority
            # if the current interaction has timed out, we can preempt it in
            # favor of lower priority ones
            and not current_interaction_timed_out
        ):  # >= means lower or equal priority (higher priority = lower number)
            return
        if not user_interaction_queues[possible_priority]:
            continue

        _logger.info(f"Preempting {current_interaction=}")
        if current_interaction.running:
            current_interaction.preempt()
        else:
            # If this task was preempted before starting to run, re-add it
            # to the queue instead of cancelling it.
            _add_to_user_interaction_queue(user_id, current_interaction)
        return

    _logger.info(
        "There are awaiting user interactions, but the current one is"
        " higher priority"
    )


def _add_to_user_interaction_queue(
    user_id: int, user_interaction: UserInteraction
) -> None:
    _get_user_interaction_queues(user_id)[user_interaction.priority].append(
        user_interaction
    )


def _remove_from_user_interaction_queue(
    user_id: int, user_interaction: UserInteraction
) -> None:
    _get_user_interaction_queues(user_id)[user_interaction.priority].remove(
        user_interaction
    )


def get_pending_interaction_count(user_id: int) -> int:
    """Number of interactions waiting in the queue (not counting current)."""
    return sum(len(queue) for queue in _get_user_interaction_queues(user_id).values())


@contextlib.asynccontextmanager
async def user_interaction(
    user_id: int,
    *,
    propagate_preemption: bool = True,
    priority: UserInteractionPriority = UserInteractionPriority.NORMAL,
) -> AsyncGenerator[None, None]:
    """Serialize conversations per user: one interaction at a time.

    Direct port of ``TelegramBotApi.user_interaction`` — same queueing,
    priority, and preemption semantics — keyed by Spanreed user id.
    """

    interaction = UserInteraction(user_id, priority)

    def log(msg: str) -> None:
        _logger.info(msg + f" {interaction=}")

    _add_to_user_interaction_queue(user_id, interaction)

    try:
        log("Waiting in queue for user interaction")
        lock: asyncio.Lock = get_user_interaction_lock(user_id)
        await _try_to_allow_next_user_interaction(user_id)
        # Wait for our turn in the queue.
        await interaction.wait_to_run()
    except asyncio.CancelledError:
        _logger.info("User interaction was cancelled")
        _remove_from_user_interaction_queue(user_id, interaction)
        raise
    log("Got user interaction permission")
    async with lock:
        try:
            asyncio.create_task(
                _try_to_allow_next_user_interaction(user_id, interaction.timeout)
            )
            yield
        except asyncio.CancelledError:
            if interaction.preempted:
                log("User interaction was preempted")
                if propagate_preemption:
                    interaction.task.uncancel()
                    raise UserInteractionPreempted()
            raise
        finally:
            _clear_current_user_interaction(user_id)
            log("Yielded user interaction")
            await _try_to_allow_next_user_interaction(user_id)


# ---------------------------------------------------------------------------
# Pending prompts (answer bookkeeping)
# ---------------------------------------------------------------------------


class PromptKind(enum.StrEnum):
    CHOICE = "choice"
    INPUT = "input"


class PromptResolution(enum.StrEnum):
    RESOLVED = "resolved"
    ALREADY_ANSWERED = "already-answered"
    UNKNOWN = "unknown"


@dataclass
class PendingPrompt:
    interaction_id: str
    user_id: int
    kind: PromptKind
    future: asyncio.Future
    generation: int = 0
    # Per-surface delivery state (e.g. the Telegram surface stores the sent
    # prompt Message here so it can delete it on re-prompt/dismissal).
    surface_state: dict[str, Any] = field(default_factory=dict)


_pending_prompts: dict[str, PendingPrompt] = {}
# user_id -> interaction_id of the prompt awaiting free-text input (a
# surface receiving free text asks the engine where to route it).
_current_input_prompt: dict[int, str] = {}
# Resolved prompt ids, so late answers get "already answered" (vs "unknown"
# for prompts from before a restart). Bounded FIFO.
_tombstones: collections.OrderedDict[str, None] = collections.OrderedDict()


def create_prompt(user_id: int, kind: PromptKind) -> PendingPrompt:
    prompt = PendingPrompt(
        interaction_id=uuid.uuid4().hex,
        user_id=user_id,
        kind=kind,
        future=asyncio.get_running_loop().create_future(),
    )
    _pending_prompts[prompt.interaction_id] = prompt
    if kind == PromptKind.INPUT:
        _current_input_prompt[user_id] = prompt.interaction_id
    return prompt


def get_prompt(interaction_id: str) -> PendingPrompt | None:
    return _pending_prompts.get(interaction_id)


def get_current_input_prompt(user_id: int) -> PendingPrompt | None:
    interaction_id = _current_input_prompt.get(user_id)
    if interaction_id is None:
        return None
    return _pending_prompts.get(interaction_id)


def resolve_prompt(interaction_id: str, value: int | str) -> PromptResolution:
    """Deliver a user's answer. First answer wins, on interaction id alone.

    The answer's generation is deliberately not validated: a surface that
    slept through re-prompt cycles still holds a valid prompt (see design
    doc §2.1).
    """
    prompt = _pending_prompts.get(interaction_id)
    if prompt is None:
        if interaction_id in _tombstones:
            return PromptResolution.ALREADY_ANSWERED
        return PromptResolution.UNKNOWN
    if prompt.future.done():
        return PromptResolution.ALREADY_ANSWERED
    prompt.future.set_result(value)
    return PromptResolution.RESOLVED


def finish_prompt(prompt: PendingPrompt) -> None:
    """Retire a prompt (answered or abandoned) and tombstone its id."""
    _pending_prompts.pop(prompt.interaction_id, None)
    if _current_input_prompt.get(prompt.user_id) == prompt.interaction_id:
        del _current_input_prompt[prompt.user_id]
    _tombstones[prompt.interaction_id] = None
    while len(_tombstones) > _TOMBSTONE_MAX:
        _tombstones.popitem(last=False)
