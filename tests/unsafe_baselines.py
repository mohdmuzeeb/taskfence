"""Test-only UNSAFE designs from the paper, used to show the window is real and the auditor sees it.

B1 PRE_EXECUTION: check when the worker starts, then work, then write.
B2 NONATOMIC_PRECOMMIT: check in one step, then write in a separate transaction.
Both record their effect in taskfence_ops so the same auditor judges them.
"""

from __future__ import annotations

from taskfence import Code, Denied, Fence
from conftest import q
from taskfence.testing import hook


def _check_outside_txn(fence: Fence, conn, token: str) -> tuple:
    claims = fence.verify(token)
    f = fence.store.fence_row(conn, claims.grant_id)
    if f is None or claims.epoch < f["min_epoch"]:
        raise Denied(Code.STALE_EPOCH)
    return claims


def b2_nonatomic_precommit(fence: Fence, conn, token: str, order: str, operation_id: str) -> int:
    claims = _check_outside_txn(fence, conn, token)
    hook("app.after_check")                      # the window: revocation can commit here
    with fence.transaction(conn):
        seq = fence.store.insert_op(conn, operation_id, claims.task_id, claims.grant_id, claims.epoch, order)
        conn.execute(q(conn, "UPDATE orders SET refunds = refunds + 1 WHERE id = ?"), (order,))
    return seq


def b1_pre_execution(fence: Fence, conn, token: str, order: str, operation_id: str) -> int:
    claims = _check_outside_txn(fence, conn, token)
    hook("app.work_interval")                    # long-running work after the only check
    with fence.transaction(conn):
        seq = fence.store.insert_op(conn, operation_id, claims.task_id, claims.grant_id, claims.epoch, order)
        conn.execute(q(conn, "UPDATE orders SET refunds = refunds + 1 WHERE id = ?"), (order,))
    return seq
