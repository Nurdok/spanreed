import asyncio

import pytest

from spanreed.interaction import engine
from spanreed.interaction.engine import (
    PromptKind,
    PromptResolution,
    UserInteractionPriority,
)
from spanreed.interaction.errors import UserInteractionPreempted

USER_ID = 7


def setup_function() -> None:
    engine.reset_state_for_tests()


# ---------------------------------------------------------------------------
# Serialization / priority / preemption
# ---------------------------------------------------------------------------


def test_user_interaction_serializes_two_tasks() -> None:
    events: list[str] = []

    async def convo(name: str, hold: asyncio.Event) -> None:
        async with engine.user_interaction(USER_ID):
            events.append(f"{name}-start")
            await hold.wait()
            events.append(f"{name}-end")

    async def run() -> None:
        hold_a = asyncio.Event()
        hold_b = asyncio.Event()
        task_a = asyncio.create_task(convo("a", hold_a))
        await asyncio.sleep(0)
        task_b = asyncio.create_task(convo("b", hold_b))
        await asyncio.sleep(0.01)
        # b must not have started while a holds the interaction.
        assert events == ["a-start"]
        hold_a.set()
        hold_b.set()
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())
    assert events == ["a-start", "a-end", "b-start", "b-end"]


def test_high_priority_runs_before_earlier_low() -> None:
    order: list[str] = []

    async def convo(
        name: str, priority: UserInteractionPriority, start: asyncio.Event
    ) -> None:
        await start.wait()
        async with engine.user_interaction(USER_ID, priority=priority):
            order.append(name)

    async def run() -> None:
        start = asyncio.Event()
        blocker_release = asyncio.Event()

        async def blocker() -> None:
            async with engine.user_interaction(
                USER_ID, priority=UserInteractionPriority.HIGH
            ):
                start.set()
                await blocker_release.wait()

        blocker_task = asyncio.create_task(blocker())
        # Queue LOW first, then HIGH, while the blocker holds the slot.
        low_task = asyncio.create_task(convo("low", UserInteractionPriority.LOW, start))
        high_task = asyncio.create_task(
            convo("high", UserInteractionPriority.HIGH, start)
        )
        await asyncio.sleep(0.01)
        blocker_release.set()
        await asyncio.gather(blocker_task, low_task, high_task)

    asyncio.run(run())
    assert order == ["high", "low"]


def test_low_priority_preempted_by_high() -> None:
    preempted = asyncio.Event()

    async def low() -> None:
        try:
            async with engine.user_interaction(
                USER_ID,
                priority=UserInteractionPriority.LOW,
                propagate_preemption=True,
            ):
                await asyncio.sleep(30)
        except UserInteractionPreempted:
            preempted.set()

    async def run() -> None:
        low_task = asyncio.create_task(low())
        await asyncio.sleep(0.01)

        async with engine.user_interaction(
            USER_ID, priority=UserInteractionPriority.HIGH
        ):
            pass

        await asyncio.wait_for(low_task, timeout=1)
        assert preempted.is_set()

    asyncio.run(run())


def test_cancellation_while_queued_cleans_up() -> None:
    async def run() -> None:
        release = asyncio.Event()

        async def blocker() -> None:
            async with engine.user_interaction(USER_ID):
                release.set()
                await asyncio.sleep(30)

        blocker_task = asyncio.create_task(blocker())
        await release.wait()

        async def queued() -> None:
            async with engine.user_interaction(USER_ID):
                pass

        queued_task = asyncio.create_task(queued())
        await asyncio.sleep(0.01)
        queued_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_task
        assert engine.get_pending_interaction_count(USER_ID) == 0
        blocker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocker_task

    asyncio.run(run())


# ---------------------------------------------------------------------------
# User-initiated flow slot: cancel-and-replace, newest wins
# ---------------------------------------------------------------------------


def test_user_flow_cancel_and_replace() -> None:
    events: list[str] = []

    async def flow(name: str) -> None:
        try:
            events.append(f"{name}-start")
            await asyncio.sleep(30)
            events.append(f"{name}-done")
        except asyncio.CancelledError:
            events.append(f"{name}-cancelled")
            raise

    async def run() -> None:
        first = engine.start_user_flow(USER_ID, flow("first"))
        await asyncio.sleep(0.01)
        second = engine.start_user_flow(USER_ID, flow("second"))
        await asyncio.sleep(0.01)
        assert first.cancelled()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    asyncio.run(run())
    assert events == [
        "first-start",
        "first-cancelled",
        "second-start",
        "second-cancelled",
    ]


def test_user_flows_are_per_user() -> None:
    async def flow() -> None:
        await asyncio.sleep(30)

    async def run() -> None:
        task_a = engine.start_user_flow(1, flow())
        task_b = engine.start_user_flow(2, flow())
        await asyncio.sleep(0.01)
        # Different users' flows coexist.
        assert not task_a.cancelled() and not task_b.cancelled()
        task_a.cancel()
        task_b.cancel()
        for task in (task_a, task_b):
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Pending prompts: first answer wins, tombstones
# ---------------------------------------------------------------------------


def test_resolve_prompt_first_answer_wins() -> None:
    async def run() -> None:
        prompt = engine.create_prompt(USER_ID, PromptKind.CHOICE)
        assert (
            engine.resolve_prompt(prompt.interaction_id, 2) == PromptResolution.RESOLVED
        )
        assert (
            engine.resolve_prompt(prompt.interaction_id, 1)
            == PromptResolution.ALREADY_ANSWERED
        )
        assert await prompt.future == 2

    asyncio.run(run())


def test_resolved_prompt_tombstoned_after_finish() -> None:
    async def run() -> None:
        prompt = engine.create_prompt(USER_ID, PromptKind.CHOICE)
        engine.resolve_prompt(prompt.interaction_id, 0)
        engine.finish_prompt(prompt)
        # Late answer after cleanup: distinguishable from a pre-restart id.
        assert (
            engine.resolve_prompt(prompt.interaction_id, 1)
            == PromptResolution.ALREADY_ANSWERED
        )
        assert engine.resolve_prompt("never-existed", 1) == PromptResolution.UNKNOWN

    asyncio.run(run())


def test_input_prompt_routing() -> None:
    async def run() -> None:
        assert engine.get_current_input_prompt(USER_ID) is None
        prompt = engine.create_prompt(USER_ID, PromptKind.INPUT)
        current = engine.get_current_input_prompt(USER_ID)
        assert current is not None
        assert current.interaction_id == prompt.interaction_id
        engine.resolve_prompt(prompt.interaction_id, "hello")
        assert await prompt.future == "hello"
        engine.finish_prompt(prompt)
        assert engine.get_current_input_prompt(USER_ID) is None

    asyncio.run(run())
