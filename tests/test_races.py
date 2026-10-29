"""Deterministic race scenarios ported from the paper's Phase 1 schedules (S1-S8).

Each worker uses its own connection. Races are placed with latches; the
independent auditor judges the final database.
"""

import random
import threading
import time

import pytest

import unsafe_baselines as unsafe
from conftest import begin, in_txn, is_pg, q, refund, refunds
from taskfence import Code, Denied, Duplicate, Retry
from taskfence.testing import assert_no_violations, audit, run_in_thread


def test_s1_no_revocation(fence, conn, connect_db, grant, taskfence_latches):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    w = run_in_thread(lambda: refund(fence, connect_db(), tok))
    assert w.outcome().epoch == 1
    assert_no_violations(conn)


def test_s2_s3_revoke_while_task_runs_is_denied(fence, conn, connect_db, grant, taskfence_latches):
    """S2/S2b/S3: revocation commits while the task works, before its guarded transaction opens.

    With fence.transaction() (BEGIN IMMEDIATE) the write lock is held from the first
    statement, so every later point is S4b: the revocation waits instead.
    """
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    p = taskfence_latches.pause_at("app.before_txn")
    w = run_in_thread(lambda: refund(fence, connect_db(), tok))
    p.wait_reached()
    assert fence.revoke(conn, grant.grant_id).effective
    p.release()
    with pytest.raises(Denied) as e:
        w.outcome()
    assert e.value.code == Code.STALE_EPOCH
    assert refunds(conn) == 0
    assert_no_violations(conn)


@pytest.mark.parametrize("read_first", [False, True])
def test_deferred_transaction_revoke_before_fence(fence, conn, connect_db, grant, taskfence_latches, read_first,
                                                 backend):
    """Caller used a deferred BEGIN, so no lock is held at guard.before_fence.

    Without a prior read, the guard's conditional UPDATE sees the revocation and denies.
    With a prior read, the transaction's WAL snapshot is stale and SQLite refuses the
    upgrade, which surfaces as Retry. Either way nothing commits.
    """
    if backend != "sqlite":
        pytest.skip("SQLite deferred-BEGIN semantics; PostgreSQL isolation is covered in test_pg_isolation.py")
    tok = fence.issue(conn, grant.grant_id, task_id="t1")

    def work():
        c = connect_db()
        c.execute("BEGIN")
        try:
            if read_first:
                c.execute("SELECT refunds FROM orders WHERE id = 'order:1'").fetchone()
            fence.guard(c, tok, operation="refund.issue", resource="order:1", operation_id="op-1")
            c.execute("UPDATE orders SET refunds = refunds + 1 WHERE id = 'order:1'")
            c.execute("COMMIT")
        finally:
            if c.in_transaction:
                c.execute("ROLLBACK")

    p = taskfence_latches.pause_at("guard.before_fence")
    w = run_in_thread(work)
    p.wait_reached()
    fence.revoke(conn, grant.grant_id)
    p.release()
    with pytest.raises(Retry if read_first else Denied):
        w.outcome()
    assert refunds(conn) == 0
    assert_no_violations(conn)


@pytest.mark.parametrize("design,point", [(unsafe.b2_nonatomic_precommit, "app.after_check"),
                                          (unsafe.b1_pre_execution, "app.work_interval")])
def test_unsafe_baselines_commit_after_revocation(fence, conn, connect_db, grant, taskfence_latches, design, point):
    """Same schedule against B1/B2: the effect commits after revocation and the auditor flags it."""
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    p = taskfence_latches.pause_at(point)
    w = run_in_thread(lambda: design(fence, connect_db(), tok, "order:1", "op-1"))
    p.wait_reached()
    fence.revoke(conn, grant.grant_id)
    p.release()
    w.outcome()
    assert refunds(conn) == 1
    v = audit(conn)
    assert [x["kind"] for x in v] == ["POST_REVOCATION"]
    with pytest.raises(AssertionError, match="1 TaskFence safety violation"):
        assert_no_violations(conn)


def test_s4_commit_then_revoke(fence, conn, connect_db, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    adm = refund(fence, connect_db(), tok)
    r = fence.revoke(conn, grant.grant_id)
    assert adm.seq < r.seq and refunds(conn) == 1
    assert_no_violations(conn)


def test_s4b_revoke_waits_for_open_guarded_transaction(fence, conn, connect_db, grant, taskfence_latches):
    """Revocation is delayed by at most one transaction, and the effect is ordered before it."""
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    admin = connect_db()
    p = taskfence_latches.pause_at("guard.after_fence")
    w = run_in_thread(lambda: refund(fence, connect_db(), tok))
    p.wait_reached()
    rv = run_in_thread(lambda: fence.revoke(admin, grant.grant_id))
    time.sleep(0.3)                                       # assertion of blocking only, not a schedule
    assert rv.is_alive(), "revoke should wait for the guarded transaction holding the write lock"
    p.release()
    adm = w.outcome()
    r = rv.outcome()
    assert adm.seq < r.seq and r.effective
    with pytest.raises(Denied):
        refund(fence, conn, tok, operation_id="later")
    assert_no_violations(conn)


def test_s5_revoke_regrant_before_stale_write(fence, conn, connect_db, grant, taskfence_latches):
    stale = fence.issue(conn, grant.grant_id, task_id="stale")
    p = taskfence_latches.pause_at("app.before_txn", match=lambda c: c["task_id"] == "stale")
    w = run_in_thread(lambda: refund(fence, connect_db(), stale))
    p.wait_reached()
    fence.revoke(conn, grant.grant_id)
    fence.regrant(conn, grant.grant_id)
    fresh = fence.issue(conn, grant.grant_id, task_id="fresh")
    refund(fence, conn, fresh)
    p.release()
    with pytest.raises(Denied) as e:
        w.outcome()
    assert e.value.code == Code.STALE_EPOCH
    assert refunds(conn) == 1
    assert_no_violations(conn)


def test_s6_duplicate_delivery_commits_once(fence, conn, connect_db, grant, taskfence_latches):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    p = taskfence_latches.pause_at("guard.after_fence")
    a = run_in_thread(lambda: refund(fence, connect_db(), tok, operation_id="op-1"))
    p.wait_reached()
    b = run_in_thread(lambda: refund(fence, connect_db(), tok, operation_id="op-1"))   # blocks on the lock
    p.release()
    results = []
    for w in (a, b):
        try:
            results.append(type(w.outcome()).__name__)
        except Duplicate:
            results.append("Duplicate")
    assert sorted(results) == ["Admission", "Duplicate"]
    assert refunds(conn) == 1


def test_s7_cancel_while_guarded_txn_open_orders_cancel_after(fence, conn, connect_db, grant, taskfence_latches):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    admin = connect_db()
    p = taskfence_latches.pause_at("guard.after_fence")
    w = run_in_thread(lambda: refund(fence, connect_db(), tok))
    p.wait_reached()
    cancel = run_in_thread(lambda: fence.cancel_task(admin, "t1"))
    p.release()
    adm = w.outcome()
    assert adm.seq < cancel.outcome()
    with pytest.raises(Denied) as e:
        refund(fence, conn, tok, "order:2")
    assert e.value.code == Code.TASK_CANCELLED
    assert_no_violations(conn)


def test_s8b_lock_held_beyond_busy_timeout_raises_retry(fence, conn, connect_db, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    holder = connect_db()
    begin(holder)
    if is_pg(holder):       # hold the fence row exclusively; lock_timeout on the worker is 200 ms
        holder.execute("SELECT 1 FROM taskfence_fences WHERE grant_id = %s FOR UPDATE", (grant.grant_id,))
    try:
        with pytest.raises(Retry):
            refund(fence, connect_db(timeout_s=0.2), tok)
    finally:
        holder.execute("ROLLBACK")
    refund(fence, conn, tok)                               # retry succeeds
    assert refunds(conn) == 1


def test_s8c_fault_before_commit_rolls_back_and_retry_is_clean(fence, conn, connect_db, grant, taskfence_latches):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    taskfence_latches.fail_at("guard.after_insert", RuntimeError("worker crashed"))
    with pytest.raises(RuntimeError):
        refund(fence, connect_db(), tok, operation_id="op-1")
    assert refunds(conn) == 0
    assert conn.execute("SELECT count(*) FROM taskfence_ops").fetchone()[0] == 0
    refund(fence, conn, tok, operation_id="op-1")          # operation_id was not burned
    assert refunds(conn) == 1


@pytest.mark.parametrize("seed", range(5))
def test_randomized_concurrency_no_post_revocation_commits(fence, conn, connect_db, grant, seed):
    """OS-scheduled threads, no latches: many guarded commits racing one revocation."""
    rng = random.Random(seed)
    tokens = [fence.issue(conn, grant.grant_id, task_id=f"t{i}") for i in range(6)]
    start = threading.Event()
    outcomes = {"committed": 0, "denied": 0, "retry": 0}
    lock = threading.Lock()

    def worker(i, tok):
        c = connect_db()
        start.wait()
        for n in range(40):
            try:
                refund(fence, c, tok, rng.choice(["order:1", "order:2"]), operation_id=f"t{i}:{n}")
                k = "committed"
            except Denied:
                k = "denied"
            except Retry:
                k = "retry"
            with lock:
                outcomes[k] += 1

    ws = [run_in_thread(lambda i=i, t=t: worker(i, t)) for i, t in enumerate(tokens)]
    start.set()
    time.sleep(rng.uniform(0.001, 0.03))
    r = fence.revoke(conn, grant.grant_id)
    for w in ws:
        w.outcome(timeout=30)
    assert r.effective
    assert_no_violations(conn)
    late = conn.execute(q(conn, "SELECT count(*) FROM taskfence_ops WHERE seq > ?"), (r.seq,)).fetchone()[0]
    assert late == 0
    assert outcomes["committed"] + outcomes["denied"] + outcomes["retry"] == 240
