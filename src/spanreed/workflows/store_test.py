import asyncio
from typing import Any

from spanreed.workflows import store as store_module
from spanreed.workflows.model import Step
from spanreed.workflows.store import WorkflowStore, emit
from spanreed.workflows.testing import fake_storage, workflow_of


def test_a_saved_workflow_is_listed_for_its_user() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        zebra = workflow_of(
            Step(id="one", block="fake.record"), id="wf_z", name="zebra"
        )
        apple = workflow_of(id="wf_a", name="Apple")
        other = workflow_of(id="wf_o", user_id=7)
        for workflow in (zebra, apple, other):
            await store.save(workflow)

        assert await store.get("wf_z") == zebra
        assert await store.get("wf_nothing") is None
        # By name, whatever the case.
        assert await store.list_for_user(4) == [apple, zebra]
        assert await store.list_for_user(7) == [other]

    with fake_storage():
        asyncio.run(scenario())


def test_deleting_a_workflow_deletes_its_state_and_its_runs() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        workflow = workflow_of()
        await store.save(workflow)
        await store.trigger_state(workflow.id).set("cursor", "5")
        await store.add_run(workflow.id, {"id": "r1"})

        await store.delete(workflow)

        assert await store.get(workflow.id) is None
        assert await store.list_for_user(4) == []
        assert await store.trigger_state(workflow.id).get("cursor") is None
        assert await store.recent_runs(workflow.id, 10) == []

    with fake_storage():
        asyncio.run(scenario())


def test_trigger_state_is_kept_for_each_workflow() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        assert await store.trigger_state("wf_1").get("cursor") is None
        await store.trigger_state("wf_1").set("cursor", "5")
        assert await store.trigger_state("wf_1").get("cursor") == "5"
        assert await store.trigger_state("wf_2").get("cursor") is None

    with fake_storage():
        asyncio.run(scenario())


def test_only_the_last_runs_are_kept_and_they_expire() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        for number in range(store_module.RUNS_KEPT + 5):
            await store.add_run("wf_1", {"id": f"r{number}"})

        runs = await store.recent_runs("wf_1", 1000)
        assert len(runs) == store_module.RUNS_KEPT
        # Newest first.
        assert runs[0] == {"id": f"r{store_module.RUNS_KEPT + 4}"}
        assert await store.recent_runs("wf_1", 2) == runs[:2]

    with fake_storage() as redis:
        asyncio.run(scenario())
        assert redis.expirations["workflows:workflow:wf_1:runs"] == 30 * 24 * 3600


# -- the queue of a workflow -------------------------------------------------


def test_keys_come_out_oldest_first_and_stay_until_done() -> None:
    async def scenario() -> None:
        queue = WorkflowStore().queue("wf_1")
        await queue.put("a")
        await queue.put("b")

        key, token = await queue.next()
        assert key == "a"
        # Not taken out yet: a restart during the run finds it again.
        assert await queue.keys() == ["a", "b"]
        assert (await queue.next())[0] == "a"

        await queue.done(key, token)
        assert await queue.keys() == ["b"]

    with fake_storage():
        asyncio.run(scenario())


def test_a_key_that_waits_is_not_queued_twice() -> None:
    async def scenario() -> None:
        queue = WorkflowStore().queue("wf_1")
        for key in ("a", "b", "a"):
            await queue.put(key)
        assert await queue.keys() == ["a", "b"]

    with fake_storage():
        asyncio.run(scenario())


def test_a_key_put_again_during_its_run_is_run_again() -> None:
    async def scenario() -> None:
        queue = WorkflowStore().queue("wf_1")
        await queue.put("a")
        await queue.put("b")
        key, token = await queue.next()

        # It changed again while the workflow ran for it.
        await queue.put("a")
        await queue.done(key, token)

        # Still there, behind the others.
        assert await queue.keys() == ["b", "a"]
        key, token = await queue.next()
        await queue.done(key, token)
        key, token = await queue.next()
        await queue.done(key, token)
        assert await queue.keys() == []

    with fake_storage():
        asyncio.run(scenario())


def test_later_moves_a_key_behind_the_others() -> None:
    async def scenario() -> None:
        queue = WorkflowStore().queue("wf_1")
        for key in ("a", "b", "c"):
            await queue.put(key)

        await queue.later("a")

        assert await queue.keys() == ["b", "c", "a"]
        # Its token is kept, so done still takes it out.
        key, token = await queue.next()
        assert key == "b"

    with fake_storage():
        asyncio.run(scenario())


def test_next_waits_for_a_key() -> None:
    async def scenario() -> None:
        queue = WorkflowStore().queue("wf_1")
        waiting = asyncio.create_task(queue.next())
        await asyncio.sleep(0)
        assert not waiting.done()

        await WorkflowStore().queue("wf_1").put("a")

        assert (await asyncio.wait_for(waiting, 1))[0] == "a"

    with fake_storage():
        asyncio.run(scenario())


def test_emit_queues_keys_for_the_workflows_that_are_on_and_use_the_trigger() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        on = workflow_of(id="wf_on", trigger="fake.pushed")
        off = workflow_of(id="wf_off", trigger="fake.pushed", enabled=False)
        other_trigger = workflow_of(id="wf_other")
        other_user = workflow_of(id="wf_user", trigger="fake.pushed", user_id=7)
        for workflow in (on, off, other_trigger, other_user):
            await store.save(workflow)

        await emit(4, "fake.pushed", ["a", "b"])
        await emit(4, "fake.pushed", [])

        assert await store.queue("wf_on").keys() == ["a", "b"]
        for workflow in (off, other_trigger, other_user):
            assert await store.queue(workflow.id).keys() == []

    with fake_storage():
        asyncio.run(scenario())


def test_deleting_a_workflow_deletes_its_queue() -> None:
    async def scenario() -> None:
        store = WorkflowStore()
        workflow = workflow_of(trigger="fake.pushed")
        await store.save(workflow)
        await store.queue(workflow.id).put("a")

        await store.delete(workflow)

        assert await store.queue(workflow.id).keys() == []

    with fake_storage() as redis:
        asyncio.run(scenario())
        assert not redis.hashes and not redis.lists


def test_the_place_of_a_run_is_kept_until_it_is_dropped() -> None:
    async def scenario(redis: Any) -> None:
        store = WorkflowStore()
        workflow = workflow_of()
        await store.save(workflow)
        assert await store.places(workflow.id) == {}

        await store.keep_place(workflow.id, "run1", {"steps": []})
        await store.keep_place(workflow.id, "run2", {"steps": [], "manual": True})
        # The newer place of a run takes the place of the older one.
        await store.keep_place(workflow.id, "run1", {"steps": [{"id": "one"}]})

        assert await store.places(workflow.id) == {
            "run1": {"steps": [{"id": "one"}]},
            "run2": {"steps": [], "manual": True},
        }
        # A place that nothing drops goes with the traces.
        key = "workflows:workflow:wf_1:places"
        assert redis.expirations[key] == 30 * 24 * 3600

        await store.drop_place(workflow.id, "run1")
        await store.drop_place(workflow.id, "run-that-is-not-there")
        assert list(await store.places(workflow.id)) == ["run2"]

        # What cannot be read is passed over.
        redis.hashes[key]["bad"] = "not json"
        redis.hashes[key]["list"] = "[1]"
        assert list(await store.places(workflow.id)) == ["run2"]

        await store.delete(workflow)
        assert await store.places(workflow.id) == {}

    with fake_storage() as redis:
        asyncio.run(scenario(redis))
