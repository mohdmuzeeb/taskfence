"""Owner-side federated messages (D5 semantics) and the transactional outbox."""

import pytest

from conftest import refund
from taskfence import Code, Denied

G = {"grant_id": "fg1", "principal": "user:42", "operation": "refund.issue"}


def install(fence, conn, msg_id, epoch, resources=("order:1",)):
    return fence.apply_install(conn, {**G, "msg_id": msg_id, "epoch": epoch, "resources": list(resources)})


def fence_msg(fence, conn, msg_id, fence_epoch):
    return fence.apply_fence(conn, {**G, "msg_id": msg_id, "fence_epoch": fence_epoch})


def tok(fence, epoch, task="t"):
    return fence.mint(**G, epoch=epoch, task_id=task)


def test_install_then_commit_then_fence(fence, conn):
    assert install(fence, conn, "m1", 1).applied
    refund(fence, conn, tok(fence, 1))
    r = fence_msg(fence, conn, "m2", 2)
    assert r.applied and r.min_epoch == 2
    with pytest.raises(Denied) as e:
        refund(fence, conn, tok(fence, 1, "t2"))
    assert e.value.code == Code.STALE_EPOCH


def test_messages_are_idempotent_and_monotone(fence, conn):
    install(fence, conn, "m1", 1)
    fence_msg(fence, conn, "m2", 3)
    again = fence_msg(fence, conn, "m2", 3)
    assert again.duplicate and again.min_epoch == 3
    stale_install = install(fence, conn, "m3", 2)               # late, lower epoch: no-op
    assert not stale_install.applied and stale_install.min_epoch == 3


def test_newer_authentic_epoch_accepted_after_lost_install(fence, conn):
    """D5 property: regrant INSTALL for epoch 2 was lost; an epoch-2 token still commits and raises the floor."""
    install(fence, conn, "m1", 1)
    fence_msg(fence, conn, "m2", 2)                            # revoke epoch 1
    refund(fence, conn, tok(fence, 2, "new"))
    assert fence.store.fence_row(conn, "fg1")["min_epoch"] == 2


def test_outbox_entry_exists_iff_guarded_txn_commits(fence, conn, grant):
    t = fence.issue(conn, grant.grant_id, task_id="t1")
    with fence.transaction(conn):
        fence.guard(conn, t, operation="refund.issue", resource="order:1", operation_id="op-1")
        fence.enqueue(conn, "op-1", "payments.refund", {"order": "order:1", "amount": 10})
    with pytest.raises(RuntimeError):
        with fence.transaction(conn):
            fence.guard(conn, t, operation="refund.issue", resource="order:2", operation_id="op-2")
            fence.enqueue(conn, "op-2", "payments.refund", {"order": "order:2"})
            raise RuntimeError("crash before commit")
    seen = []
    assert fence.drain_outbox(conn, seen.append) == 1
    assert [e["operation_id"] for e in seen] == ["op-1"]
    assert fence.drain_outbox(conn, seen.append) == 0


def test_enqueue_requires_admitted_operation(fence, conn):
    from taskfence import UsageError
    with fence.transaction(conn):
        with pytest.raises(UsageError):
            fence.enqueue(conn, "never-guarded", "x", {})
