import asyncio

import pytest
from harness_protocol import EnableChange, HarnessError
from harness_runtime.examples import demo_definition, demo_registry
from harness_runtime.jobs import SessionJobManager
from harness_runtime.session import SessionManager


async def test_manager_cancels_and_drains_tasks_per_binding():
    manager = SessionJobManager()
    stopped = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    manager.register("one", 1, work())
    manager.register("two", 1, asyncio.sleep(60))
    await asyncio.sleep(0)
    assert manager.busy_bindings() == {"one", "two"}
    assert await manager.cancel_binding("one") is True
    assert stopped.is_set()
    assert manager.busy_bindings() == {"two"}
    assert await manager.close(timeout=0.5) is True
    assert manager.busy_bindings() == set()


async def test_cancellation_suppressing_task_stays_busy_then_drains():
    manager = SessionJobManager()
    release = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await release.wait()

    manager.register("binding", 1, stubborn())
    await asyncio.sleep(0)
    assert await manager.cancel_binding("binding", timeout=0.01) is False
    assert manager.has_active("binding")
    release.set()
    await asyncio.sleep(0)
    assert await manager.cancel_binding("binding", timeout=1) is True


async def test_external_jobs_require_acknowledged_cancel_or_completion():
    manager = SessionJobManager()
    unknown = manager.register_external("provider", 1, description="remote batch")
    assert await manager.cancel_binding("provider", timeout=0.01) is False
    manager.complete(unknown)
    assert await manager.cancel_binding("provider") is True

    async def cancel():
        return None

    manager.register_external("provider", 1, cancel=cancel)
    assert await manager.cancel_binding("provider") is True

    def broken():
        raise RuntimeError("provider API rejected cancellation")

    manager.register_external("provider", 1, cancel=broken)
    assert await manager.cancel_binding("provider") is False


async def _open_demo(tmp_path):
    definition = demo_definition()
    manager = SessionManager(
        demo_registry(include_catalog=False),
        store=tmp_path / "jobs.sqlite3",
        definitions=[definition],
        allow_local_binding_creation=False,
    )
    return manager, await manager.open_session(definition, principal="tester", deployment_id="local")


async def test_disabling_binding_rejects_active_external_work_then_allows_completion(tmp_path):
    manager, session = await _open_demo(tmp_path)
    try:
        job = session.register_external_job("tone", description="provider job")
        with pytest.raises(HarnessError) as error:
            await session.update_configuration(
                [EnableChange(binding_id="tone", enabled=False)],
                request_id="disable-1",
                expected_revision=session.snapshot.selected_revision,
            )
        assert error.value.code == "CAPABILITY_BUSY"
        assert session.snapshot.bindings["tone"].enabled is True

        session.jobs.complete(job)
        snapshot = await session.update_configuration(
            [EnableChange(binding_id="tone", enabled=False)],
            request_id="disable-2",
            expected_revision=session.snapshot.selected_revision,
        )
        assert snapshot.bindings["tone"].enabled is False

        # Even a direct registration cannot smuggle work into a disabled binding:
        # a turn refuses to start while it still owns that work.
        session.jobs.register_external("tone", session.snapshot.selected_revision)
        with pytest.raises(HarnessError) as busy:
            await session.run("hello")
        assert busy.value.code == "CAPABILITY_BUSY"
    finally:
        await session.close()
        await manager.close()


async def test_disabling_binding_cancels_its_owned_task(tmp_path):
    manager, session = await _open_demo(tmp_path)
    try:
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(60)

        session.register_job("tone", work(), description="background refinement")
        await asyncio.wait_for(started.wait(), 1)
        snapshot = await session.update_configuration(
            [EnableChange(binding_id="tone", enabled=False)],
            request_id="cancel-owned",
            expected_revision=session.snapshot.selected_revision,
        )
        assert snapshot.bindings["tone"].enabled is False
        assert session.jobs.busy_bindings() == set()
    finally:
        await session.close()
        await manager.close()
