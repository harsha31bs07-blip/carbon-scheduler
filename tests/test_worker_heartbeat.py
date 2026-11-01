"""A real background SQLite heartbeat survives long calls and stops on loss."""

from datetime import timedelta
import threading
import time
from uuid import uuid4

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt, SystemClock
from workflow.queue_store import LeaseError, QueueStore
from workflow import worker
from workflow.workload import KnowledgeBase


TICKET = "My CarbonDesk account is locked after failed login attempts and I cannot reset my password."


class ControlledClock:
    """Advance lease time only after the real renewal thread acknowledges it."""

    def __init__(self):
        self._time = SystemClock().now()
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self._time

    def advance(self, seconds):
        with self._lock:
            self._time += timedelta(seconds=seconds)


@pytest.fixture
def queue(tmp_path):
    registry = DeploymentRegistry([Deployment("local", "ollama", "http://localhost:11434", {"small": "tiny:3b"})])
    kb, store, clock = KnowledgeBase.load(), QueueStore(tmp_path / "queue.sqlite3"), ControlledClock()
    worker.submit_fixed(store, registry, kb, TICKET, deployment_id="local", model_id="small",
                        clock=clock, workflow_id="ticket")
    yield store, registry, kb, clock
    store.close()


def capture_heartbeat(monkeypatch):
    active = []
    original = worker._Heartbeat.__enter__

    def enter(self):
        active.append(self)
        return original(self)

    monkeypatch.setattr(worker._Heartbeat, "__enter__", enter)
    return active


def test_actual_background_connection_renews_short_lease_during_provider_wait(queue, monkeypatch):
    store, registry, kb, clock = queue
    active = capture_heartbeat(monkeypatch)
    renewed, extended, renewals = threading.Event(), threading.Event(), []
    lease_seconds = 1.5
    original_expiry = clock.now() + timedelta(seconds=lease_seconds)
    original = QueueStore.renew
    main_thread = threading.get_ident()
    original_connection = store.connection

    def renew(self, claim, **kwargs):
        result = original(self, claim, **kwargs)
        renewals.append((threading.get_ident(), self.connection, result.lease_expires_at))
        renewed.set()
        if result.lease_expires_at > original_expiry:
            extended.set()
        return result

    monkeypatch.setattr(QueueStore, "renew", renew)
    calls = []

    def dispatch(candidate, registry, prompt, **settings):
        calls.append(candidate.step_id)
        started = clock.now()
        assert renewed.wait(2), "Background connection did not renew its first lease"
        # Startup cannot expire a lease: logical time remains frozen until the
        # actual SQLite renewal succeeds. Renew once at t=1, then inspect at t=2
        # after the original t=1.5 expiry and before the renewed t=2.5 expiry.
        clock.advance(1)
        assert extended.wait(2), "Background connection did not extend the advanced lease"
        clock.advance(1)
        time.sleep(.2)  # Inference is still in progress while the heartbeat owns its connection.
        assert clock.now() > original_expiry
        observer = QueueStore(store.path)
        try:
            observer.recover(now=clock.now())
            assert observer.read("ticket").jobs[0].status == "running"
            assert observer.claim("competitor", now=clock.now(), lease_seconds=1) is None
        finally:
            observer.close()
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "completed",
            actual_start=started, actual_finish=clock.now(), output="account_access",
            executed_model_id="tiny:3b", executed_deployment_id="local", input_tokens=30, output_tokens=3,
            output_limit_tokens=settings["output_limit_tokens"], enable_thinking=settings["enable_thinking"],
            completion_reason="stop")

    worker.work_once(store, worker_id="owner", registry=registry, kb=kb, clock=clock,
                     dispatcher=dispatch, lease_seconds=lease_seconds, heartbeat=True)
    assert calls == ["classify"] and store.read("ticket").jobs[0].status == "completed"
    assert renewals and all(thread != main_thread and connection is not original_connection
                            for thread, connection, expiry in renewals)
    assert active[0].stop.is_set() and not active[0].thread.is_alive()


def test_heartbeat_lease_loss_stops_thread_and_never_redispatches_ambiguous_call(queue, monkeypatch):
    store, registry, kb, clock = queue
    active = capture_heartbeat(monkeypatch)
    lost = threading.Event()
    calls = []

    def lose_lease(self, claim, **kwargs):
        assert self.connection is not store.connection
        lost.set()
        raise LeaseError("lease was lost")

    monkeypatch.setattr(QueueStore, "renew", lose_lease)

    def interrupted(candidate, registry, prompt, **settings):
        calls.append(candidate.step_id)
        assert lost.wait(2), "Heartbeat did not observe lease loss"
        active[0].thread.join(timeout=2)
        assert not active[0].thread.is_alive()
        raise RuntimeError("provider accepted request before worker interruption")

    with pytest.raises(RuntimeError, match="provider accepted"):
        worker.work_once(store, worker_id="owner", registry=registry, kb=kb, clock=clock,
                         dispatcher=interrupted, lease_seconds=1.5, heartbeat=True)
    assert active[0].stop.is_set() and not active[0].thread.is_alive()
    assert store.read("ticket").jobs[0].status == "running"
    # Recovery observes a time beyond the last lease; no wall-clock wait is
    # needed to establish ambiguity after the heartbeat has already stopped.
    class AfterExpiry:
        def now(self):
            return clock.now() + timedelta(seconds=2)

    result = worker.work_once(store, worker_id="replacement", clock=AfterExpiry(), heartbeat=True,
        dispatcher=lambda *a, **k: pytest.fail("Lease loss must not automatically repeat an external call"))
    assert result == {"status": "idle"} and calls == ["classify"]
    assert store.read("ticket").status == "needs-reconciliation"
