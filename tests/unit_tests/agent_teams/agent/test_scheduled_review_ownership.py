# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Original review tools/board plus Provider construction and exit ownership."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.agent_teams.agent.coordination.kernel import CoordinationKernel
from openjiuwen.agent_teams.agent.runtime_factory import require_member_runtime_factory
from openjiuwen.agent_teams.harness.team_harness import TeamHarness
from openjiuwen.agent_teams.schema.task import TaskGraphSpec
from tests.unit_tests.agent_teams.agent.test_team_scheduler import (
    TEAM, _build_scheduler, _reviewer_mgr, bus, db,
)
from tests.unit_tests.agent_teams.test_coordination_lifecycle import _make_kernel_host
from tests.unit_tests.agent_teams.test_member_provider_construction import Factory, spec_for


class Review:
    def __init__(self, request):
        self.request = request
        self.interaction_owner = 'review:' + getattr(request, 'invocation_id', 'native-test')
        self.provider_id = request.spec.execution_provider
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.disposing = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.fail_cleanup = False
        self.fail_run = False
        self.calls = 0
        self.closed = False

    async def run_once(self, prompt):
        self.calls += 1
        self.prompt = prompt
        self.entered.set()
        await self.release.wait()
        if self.fail_run:
            raise RuntimeError("unknown provider result")
        return "review complete"

    async def dispose(self):
        self.disposing.set()
        await self.cleanup_release.wait()
        if self.fail_cleanup:
            raise RuntimeError("native exit unconfirmed")
        self.closed = True


class ReviewFactory(Factory):
    def __init__(self, provider="codex"):
        super().__init__(provider)
        self.reviews = []

    def build_review_runtime(self, request):
        review = Review(request)
        self.reviews.append(review)
        return review


def scheduler_for(db, bus, factory, monkeypatch):
    scheduler, host, _, tm = _build_scheduler(db, bus)
    scheduler._blueprint.spec = spec_for(
        factory, team_name=TEAM, dispatch_mode="scheduled", execution_provider=factory.provider_id,
    )
    scheduler._blueprint.language = "en"
    scheduler._infra.team_backend = SimpleNamespace(
        team_name=TEAM, db=db, workspace_cache=None, task_verification_enabled=lambda: True,
    )
    scheduler._infra.messager = bus
    host.session_manager = SimpleNamespace(team_session=object())
    host.stream_controller = SimpleNamespace(stream_queue=asyncio.Queue())
    monkeypatch.setattr(TeamHarness, "build", Mock(side_effect=AssertionError("Native fallback")))
    return scheduler, tm


async def seed(db, bus, tm, reviewers=("rev-1",)):
    assert (await tm.add_graph([TaskGraphSpec(
        task_id="work", title="Work", content="Review output", assignee="dev-1", reviewer=reviewers,
    )])).ok
    assert (await tm.start_task("work")).ok
    author = _reviewer_mgr(db, bus, "dev-1")
    assert (await author.complete("work")).ok
    return await tm.get("work")


async def review_started(factory, count=1):
    async with asyncio.timeout(2):
        while len(factory.reviews) < count:
            await asyncio.sleep(0)
        await factory.reviews[count - 1].entered.wait()
    return factory.reviews[count - 1]


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_scheduled_factory_required_before_construction(provider):
    factory = Factory(provider)
    with pytest.raises(ValueError, match="review runtime factory"):
        require_member_runtime_factory(spec_for(factory, execution_provider=provider, dispatch_mode="scheduled"))
    assert factory.requests == []
    assert require_member_runtime_factory(spec_for(factory, execution_provider=provider)) is factory


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_original_vote_does_not_settle_before_review_exit(db, bus, monkeypatch, provider):
    factory = ReviewFactory(provider)
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    task = await seed(db, bus, tm)
    await scheduler.activate()
    review = await review_started(factory)
    request = review.request
    assert scheduler.review_interaction_target(review.interaction_owner) is review
    assert scheduler.review_interaction_target('other') is None
    assert request.spec.execution_provider == provider
    assert request.reviewer == "rev-1" and request.task_id == "work" and request.review_round == task.review_round
    assert request.invocation_id and request.team_session is scheduler._host.session_manager.team_session
    chunk = object()
    queue = scheduler._host.stream_controller.stream_queue
    await request.output_sink(chunk)
    assert queue.get_nowait() is chunk
    scheduler._host.stream_controller.stream_queue = asyncio.Queue()
    with pytest.raises(RuntimeError, match="owner"):
        await request.output_sink(chunk)
    assert scheduler._host.stream_controller.stream_queue.empty()
    scheduler._host.stream_controller.stream_queue = queue
    session = scheduler._host.session_manager.team_session
    scheduler._host.session_manager.team_session = object()
    with pytest.raises(RuntimeError, match="owner"):
        await request.output_sink(chunk)
    scheduler._host.session_manager.team_session = session
    assert [tool.card.name for tool in request.tools] == ["verify_task", "view_task"]
    assert "rev-1" in request.system_prompt
    viewed = await request.tools[1].invoke({"action": "get", "task_id": "work"})
    assert viewed.success and viewed.data["task_id"] == "work"
    pending = await request.tools[1].invoke({"action": "in_review"})
    assert pending.success and "work" in str(pending.data)
    result = await request.tools[0].invoke({"task_id": "work", "decision": "pass"})
    assert result.success
    await scheduler._scan()
    assert (await tm.get("work")).status == "in_review"
    review.cleanup_release.clear()
    review.release.set()
    await asyncio.wait_for(review.disposing.wait(), 2)
    await scheduler._scan()
    assert (await tm.get("work")).status == "in_review"
    owned = list(scheduler._review_runs.values())[0].task
    review.cleanup_release.set()
    await asyncio.wait_for(owned, 2)
    assert review.closed and (await tm.get("work")).status == "completed"
    assert len(factory.reviews) == 1
    assert scheduler.review_interaction_target(review.interaction_owner) is None
    await scheduler.stop_reviewers()


@pytest.mark.asyncio
async def test_pending_review_scan_only_dispatches_missing_votes(db, bus, monkeypatch):
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    await seed(db, bus, tm, reviewers=("rev-1", "rev-2"))
    assert (await _reviewer_mgr(db, bus, "rev-1").verify_task("work", "pass", "already done")).ok
    await scheduler.activate()
    review = await review_started(factory)
    assert review.request.reviewer == "rev-2"
    await scheduler._scan()
    assert len(factory.reviews) == 1
    await scheduler.stop_reviewers()
    assert review.closed and not scheduler._review_runs


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "timeout", "cancel-waiter"])
async def test_cleanup_failure_timeout_or_cancel_retains_original_owner(db, bus, monkeypatch, mode):
    import openjiuwen.agent_teams.agent.scheduling.scheduler as module
    monkeypatch.setattr(module, "_REVIEW_DRAIN_TIMEOUT_SECONDS", .03)
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    task = await seed(db, bus, tm)
    await scheduler.activate()
    review = await review_started(factory)
    if mode == "failure":
        review.fail_cleanup = True
    else:
        review.cleanup_release.clear()
    stopping = asyncio.create_task(scheduler.stop_reviewers())
    await asyncio.wait_for(review.disposing.wait(), 2)
    if mode == "cancel-waiter":
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    else:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await stopping
    assert scheduler._review_runs and not review.closed
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await scheduler.activate()
    await scheduler._dispatch_to_reviewer("rev-1", task)
    assert len(factory.reviews) == 1
    review.fail_cleanup = False
    review.cleanup_release.set()
    await scheduler.stop_reviewers()
    assert review.closed and review.calls == 1 and not scheduler._review_runs


@pytest.mark.asyncio
async def test_external_unknown_result_is_not_replayed_by_board_scan(db, bus, monkeypatch):
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    await seed(db, bus, tm)
    await scheduler.activate()
    review = await review_started(factory)
    review.fail_run = True
    owned = list(scheduler._review_runs.values())[0].task
    review.release.set()
    await owned
    await scheduler._scan()
    assert len(factory.reviews) == 1 and review.closed
    assert (await tm.get("work")).status == "in_review"
    await scheduler.stop_reviewers()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "stop"])
async def test_kernel_preserves_session_and_resources_until_review_cleanup(db, bus, monkeypatch, operation):
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    await seed(db, bus, tm)
    await scheduler.activate()
    review = await review_started(factory)
    review.fail_cleanup = True
    host = _make_kernel_host()
    kernel = CoordinationKernel(host)
    kernel._lifecycle_state = "running"
    kernel._scheduler = scheduler
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await getattr(kernel, operation)()
    host.session_manager.release_session.assert_not_called()
    host.resources.harness.dispose.assert_not_awaited()
    assert kernel._lifecycle_state == "running"
    review.fail_cleanup = False
    await getattr(kernel, operation)()
    host.session_manager.release_session.assert_called_once()
    assert review.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["wrong-provider", "missing-session"])
async def test_invalid_review_binding_never_dispatches(db, bus, monkeypatch, reason):
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    task = await seed(db, bus, tm)
    original = factory.build_review_runtime

    def build(request):
        runtime = original(request)
        runtime.provider_id = "native"
        return runtime

    if reason == "wrong-provider":
        monkeypatch.setattr(factory, "build_review_runtime", build)
    else:
        scheduler._host.session_manager.team_session = None
    await scheduler._dispatch_to_reviewer("rev-1", task)
    await list(scheduler._review_runs.values())[0].task
    assert not any(runtime.calls for runtime in factory.reviews)
    assert all(runtime.closed for runtime in factory.reviews)
    await scheduler.stop_reviewers()


@pytest.mark.asyncio
async def test_native_retry_disposes_before_rebuild_and_retains_legacy_tools(db, bus, monkeypatch):
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    scheduler._blueprint.spec.execution_provider = "native"
    task = await seed(db, bus, tm)
    built = []

    def build(**kwargs):
        assert not built or built[-1].closed
        instance = Review(SimpleNamespace(spec=scheduler._blueprint.spec))
        instance.release.set()
        async def once(prompt):
            instance.calls += 1
            return "181001 transient" if len(built) == 1 else "done"
        instance.run_once = once
        built.append(instance)
        assert [tool.card.name for tool in kwargs["agent_spec"].tools][-2:] == ["verify_task", "view_task"]
        return instance

    monkeypatch.setattr(TeamHarness, "build", build)
    await scheduler._dispatch_to_reviewer("rev-1", task)
    await list(scheduler._review_runs.values())[0].task
    assert len(built) == 2 and all(runtime.closed and runtime.calls == 1 for runtime in built)
    assert not factory.reviews
    await scheduler.stop_reviewers()


@pytest.mark.asyncio
async def test_native_pause_rescan_keeps_votes_and_restarts_only_unfinished_review(db, bus, monkeypatch):
    factory = ReviewFactory()
    scheduler, tm = scheduler_for(db, bus, factory, monkeypatch)
    scheduler._blueprint.spec.execution_provider = "native"
    await seed(db, bus, tm, reviewers=("rev-1", "rev-2"))
    assert (await _reviewer_mgr(db, bus, "rev-1").verify_task("work", "pass")).ok
    built = []

    def build(**kwargs):
        runtime = Review(SimpleNamespace(spec=scheduler._blueprint.spec))
        assert kwargs["member_name"] == "rev-2"
        built.append(runtime)
        return runtime

    monkeypatch.setattr(TeamHarness, "build", build)
    await scheduler.activate()
    async with asyncio.timeout(2):
        while not built:
            await asyncio.sleep(0)
        await built[0].entered.wait()
    await scheduler.stop_reviewers()
    assert built[0].closed
    await scheduler.activate()
    async with asyncio.timeout(2):
        while len(built) < 2:
            await asyncio.sleep(0)
        await built[1].entered.wait()
    await scheduler.stop_reviewers()
    assert all(runtime.closed for runtime in built)
