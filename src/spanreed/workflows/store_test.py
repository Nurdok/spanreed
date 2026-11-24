import asyncio

from spanreed.workflows import store as store_module
from spanreed.workflows.model import Step
from spanreed.workflows.store import WorkflowStore
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
