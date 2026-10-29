"""PostgreSQL-only evidence: isolation levels and revocation liveness (DESIGN.md §6)."""

import threading
import time

import pytest

from conftest import refunds
from taskfence import Denied, Retry
from taskfence.testing import assert_no_violations, run_in_thread

LEVELS = ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]


@pytest.fixture
def pg(backend):
    if not backend.startswith("pg"):
        pytest.skip("PostgreSQL only")
    return backend


def _isolated(connect_db, level):
    from psycopg import IsolationLevel
    c = connect_db()
    c.isolation_level = getattr(IsolationLevel, level.replace(" ", "_"))
    return c


@pytest.mark.parametrize("level", LEVELS)
def test_transaction_uses_connection_isolation(pg, fence, conn, connect_db, level):
    c = _isolated(connect_db, level)
    with fence.transaction(c):
        assert c.execute("SHOW transaction_isolation").fetchone()[0] == level.lower()


@pytest.mark.parametrize("level", LEVELS)
def test_revoke_after_snapshot_never_commits(pg, fence, conn, connect_db, grant, taskfence_latches, level):
    """The worker's snapshot predates the revocation. RC re-reads (Denied); RR/SERIALIZABLE abort (Retry)."""
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    c = _isolated(connect_db, level)

    def work():
        with fence.transaction(c):
            c.execute("SELECT refunds FROM orders WHERE id = 'order:1'").fetchone()    # takes the snapshot
            fence.guard(c, tok, operation="refund.issue", resource="order:1", operation_id="op-1")
            c.execute("UPDATE orders SET refunds = refunds + 1 WHERE id = 'order:1'")

    p = taskfence_latches.pause_at("guard.before_fence")
    w = run_in_thread(work)
    p.wait_reached()
    fence.revoke(conn, grant.grant_id)
    p.release()
    with pytest.raises(Denied if level == "READ COMMITTED" else Retry):
        w.outcome()
    assert refunds(conn) == 0
    assert_no_violations(conn)


@pytest.mark.parametrize("level", LEVELS)
def test_cancel_after_snapshot_never_commits(pg, fence, conn, connect_db, grant, taskfence_latches, level):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    c = _isolated(connect_db, level)
    # The task row exists already (an earlier operation), so the guard must lock and re-read it.
    with fence.transaction(c):
        fence.guard(c, tok, operation="refund.issue", resource="order:2", operation_id="op-0")

    def work():
        with fence.transaction(c):
            c.execute("SELECT 1").fetchone()
            c.execute("SELECT count(*) FROM taskfence_tasks").fetchone()               # snapshot
            fence.guard(c, tok, operation="refund.issue", resource="order:1", operation_id="op-1")

    p = taskfence_latches.pause_at("guard.before_fence", match=lambda x: x["operation_id"] == "op-1")
    w = run_in_thread(work)
    p.wait_reached()
    fence.cancel_task(conn, "t1")
    p.release()
    with pytest.raises((Denied, Retry)):
        w.outcome()
    assert_no_violations(conn)


def _revoke_latency_under_load(fence, conn, connect_db, grant, *, threads=8, hold_s=0.02, run_s=3.0) -> float:
    """Guards from many threads keep the grant locked continuously; how long does revoke() wait?"""
    tokens = [fence.issue(conn, grant.grant_id, task_id=f"t{i}") for i in range(threads)]
    stop = threading.Event()

    def worker(i, tok):
        c = connect_db()
        n = 0
        while not stop.is_set():
            try:
                with fence.transaction(c):
                    fence.guard(c, tok, operation="refund.issue", resource="order:1", operation_id=f"t{i}:{n}")
                    time.sleep(hold_s)
            except (Denied, Retry):
                pass
            n += 1

    ws = [run_in_thread(lambda i=i, t=t: worker(i, t)) for i, t in enumerate(tokens)]
    time.sleep(0.2)                                   # let the stream of overlapping guards build up
    admin = connect_db(timeout_s=run_s)
    t0 = time.monotonic()
    try:
        fence.revoke(admin, grant.grant_id)
        latency = time.monotonic() - t0
    except Retry:
        latency = float("inf")                        # lock_timeout hit: revoke was starved
    stop.set()
    for w in ws:
        w.outcome(timeout=30)
    assert_no_violations(conn)
    return latency


def test_revoke_is_not_starved_by_continuous_guards(pg, fence, conn, connect_db, grant):
    latency = _revoke_latency_under_load(fence, conn, connect_db, grant)
    print(f"\n[{pg}] revoke latency under continuous guards: {latency * 1000:.0f} ms")
    assert latency < 1.0


def test_plain_for_share_revoke_latency_for_comparison(pg, conn, connect_db, grant):
    if pg != "pg-shared":
        pytest.skip("measured once")
    """Why shared mode adds an advisory lock: plain FOR SHARE has no fair queue, so revoke latency is erratic."""
    from taskfence import Fence, KeyRing
    from taskfence.stores.postgres import PostgresStore

    class PlainForShare(PostgresStore):
        def _grant_lock(self, conn, grant_id, *, exclusive):
            pass

    f = Fence(KeyRing.generate(), PlainForShare(lock="shared"))
    latency = _revoke_latency_under_load(f, conn, connect_db, grant)
    print(f"\n[plain FOR SHARE] revoke latency under continuous guards: "
          f"{'starved (>3 s)' if latency == float('inf') else f'{latency * 1000:.0f} ms'}")
