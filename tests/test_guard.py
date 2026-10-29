"""Single-threaded behaviour of guard() and the control plane."""

import pytest

from conftest import begin, in_txn, q, refund, refunds
from taskfence import Code, Denied, Duplicate, UsageError
from taskfence.testing import assert_no_violations


def denied(fn, *a, **kw) -> Code:
    with pytest.raises(Denied) as e:
        fn(*a, **kw)
    return e.value.code


def test_happy_path(fence, conn, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    adm = refund(fence, conn, tok)
    assert (adm.grant_id, adm.epoch, adm.task_id) == (grant.grant_id, 1, "t1")
    assert refunds(conn) == 1
    assert_no_violations(conn)


def test_denied_rolls_back_the_effect(fence, conn, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    fence.revoke(conn, grant.grant_id)
    with pytest.raises(Denied):
        with fence.transaction(conn):
            conn.execute("UPDATE orders SET refunds = refunds + 1 WHERE id = 'order:1'")   # effect first
            fence.guard(conn, tok, operation="refund.issue", resource="order:1", operation_id="x")
    assert refunds(conn) == 0
    assert not in_txn(conn)


def test_guard_requires_open_transaction(fence, conn, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    with pytest.raises(UsageError):
        fence.guard(conn, tok, operation="refund.issue", resource="order:1", operation_id="x")


def test_transaction_refuses_nesting(fence, conn):
    conn.execute("BEGIN")
    with pytest.raises(UsageError):
        with fence.transaction(conn):
            pass
    conn.execute("ROLLBACK")


@pytest.mark.parametrize("resource", ["order:3", None])
def test_resource_scope(fence, conn, grant, resource):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    assert denied(refund, fence, conn, tok, resource) == Code.RESOURCE_NOT_IN_SCOPE


def test_wildcard_scope(fence, conn):
    g = fence.grant(conn, principal="user:42", operation="refund.issue", resources=["*"])
    refund(fence, conn, fence.issue(conn, g.grant_id, task_id="t1"), "order:3")
    assert refunds(conn, "order:3") == 1


def test_operation_mismatch(fence, conn, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    with fence.transaction(conn):
        code = denied(fence.guard, conn, tok, operation="order.delete", resource="order:1", operation_id="x")
    assert code == Code.OPERATION_MISMATCH


def test_forged_principal_cannot_be_minted_without_key(fence, conn, grant):
    # A token signed by the right key but naming another principal is rejected by the fence row.
    tok = fence.mint(grant_id=grant.grant_id, principal="user:7", operation="refund.issue", epoch=1, task_id="t")
    assert denied(refund, fence, conn, tok) == Code.PRINCIPAL_MISMATCH


def test_future_epoch_rejected_in_embedded_mode(fence, conn, grant):
    tok = fence.mint(grant_id=grant.grant_id, principal="user:42", operation="refund.issue", epoch=99, task_id="t")
    assert denied(refund, fence, conn, tok) == Code.FUTURE_EPOCH


def test_unknown_grant(fence, conn):
    tok = fence.mint(grant_id="nope", principal="user:42", operation="refund.issue", epoch=1, task_id="t")
    assert denied(refund, fence, conn, tok) == Code.NO_GRANT


def test_issue_refuses_revoked_grant(fence, conn, grant):
    fence.revoke(conn, grant.grant_id)
    assert denied(fence.issue, conn, grant.grant_id, task_id="t") == Code.STALE_EPOCH


def test_revoke_is_idempotent(fence, conn, grant):
    a = fence.revoke(conn, grant.grant_id)
    b = fence.revoke(conn, grant.grant_id)
    assert a.effective and b.effective and a.min_epoch == b.min_epoch == 2


def test_revoke_inside_caller_transaction_is_not_yet_effective(fence, conn, grant):
    begin(conn)
    r = fence.revoke(conn, grant.grant_id)
    assert r.effective is False
    conn.execute("ROLLBACK")
    refund(fence, conn, fence.issue(conn, grant.grant_id, task_id="t"))   # rollback undid the revoke


def test_regrant_kills_old_epoch_and_admits_new(fence, conn, grant):
    old = fence.issue(conn, grant.grant_id, task_id="old")
    g2 = fence.regrant(conn, grant.grant_id)                 # without a prior revoke
    assert g2.epoch == 2
    assert denied(refund, fence, conn, old) == Code.STALE_EPOCH
    refund(fence, conn, fence.issue(conn, grant.grant_id, task_id="new"))
    fence.revoke(conn, grant.grant_id)
    assert fence.regrant(conn, grant.grant_id, resources=["order:2"]).resources == ("order:2",)
    assert denied(refund, fence, conn, fence.issue(conn, grant.grant_id, task_id="n3"), "order:1") \
        == Code.RESOURCE_NOT_IN_SCOPE
    assert_no_violations(conn)


def test_task_cancellation_is_fenced(fence, conn, grant):
    """Closes Phase 1 limitation #4: a cancelled task can no longer commit."""
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    fence.cancel_task(conn, "t1")
    assert denied(refund, fence, conn, tok) == Code.TASK_CANCELLED
    refund(fence, conn, fence.issue(conn, grant.grant_id, task_id="t2"))


def test_duplicate_and_cancelled_operations(fence, conn, grant):
    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    refund(fence, conn, tok, operation_id="op-1")
    with pytest.raises(Duplicate):
        refund(fence, conn, tok, operation_id="op-1")
    assert refunds(conn) == 1
    assert fence.cancel_operation(conn, "op-2", task_id="t1") is True
    assert denied(refund, fence, conn, tok, operation_id="op-2") == Code.OPERATION_CANCELLED
    assert fence.cancel_operation(conn, "op-1", task_id="t1") is False      # already committed


def test_predicate(fence, conn):
    g = fence.grant(conn, principal="user:42", operation="refund.issue", resources=["*"])
    tok = fence.issue(conn, g.grant_id, task_id="t")

    def owns_order(c, claims, resource):
        row = c.execute(q(c, "SELECT customer FROM orders WHERE id = ?"), (resource,)).fetchone()
        return row is not None and row[0] == claims.principal

    with fence.transaction(conn):
        fence.guard(conn, tok, operation="refund.issue", resource="order:1", operation_id="a", predicate=owns_order)
    with fence.transaction(conn):
        code = denied(fence.guard, conn, tok, operation="refund.issue", resource="order:3", operation_id="b",
                      predicate=owns_order)
    assert code == Code.PREDICATE_FAILED


def test_fenced_decorator(fence, conn, grant):
    @fence.fenced(operation="refund.issue", resource=lambda order_id, **_: order_id,
                  operation_id=lambda task_id, order_id, **_: f"{task_id}:{order_id}")
    def issue_refund(c, *, admission, task_id, order_id):
        c.execute(q(c, "UPDATE orders SET refunds = refunds + 1 WHERE id = ?"), (order_id,))
        return admission.seq

    tok = fence.issue(conn, grant.grant_id, task_id="t1")
    assert issue_refund(conn, token=tok, task_id="t1", order_id="order:1") > 0
    fence.revoke(conn, grant.grant_id)
    with pytest.raises(Denied):
        issue_refund(conn, token=tok, task_id="t1", order_id="order:2")
    assert (refunds(conn, "order:1"), refunds(conn, "order:2")) == (1, 0)


def test_grant_requires_resources(fence, conn):
    with pytest.raises(ValueError):
        fence.grant(conn, principal="p", operation="o", resources=[])


def test_federated_apis_cannot_touch_embedded_grant(fence, conn, grant):
    with pytest.raises(UsageError):
        fence.apply_fence(conn, {"msg_id": "m", "grant_id": grant.grant_id, "principal": "user:42",
                                 "operation": "refund.issue", "fence_epoch": 5})
