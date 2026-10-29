"""Shared pieces of the refund-desk example: schema, the Fence, and the protected effect.

Everything here is synthetic. No real payments are made; the "payment" is an
outbox entry that a separate relay would deliver.
"""

from __future__ import annotations

import os
from pathlib import Path

from taskfence import Fence, KeyRing
from taskfence.stores.sqlite import connect

DB = Path(os.environ.get("TASKFENCE_DB", Path(__file__).with_name("refund_desk.sqlite")))
PRINCIPAL = os.environ.get("REFUND_DESK_USER", "user:42")

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, customer TEXT NOT NULL, total_cents INTEGER NOT NULL,
    refunded_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS refund_tasks (
    task_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('working', 'committed', 'denied', 'failed')),
    code TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS demo_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO orders VALUES
    ('order:1001', 'user:42', 5000, 0),
    ('order:1002', 'user:42', 1200, 0),
    ('order:2001', 'user:7',  9900, 0);
"""


def keys() -> KeyRing:
    """Production: set TASKFENCE_KEYS. The demo generates a key per server process."""
    return KeyRing.from_env() if os.environ.get("TASKFENCE_KEYS") else KeyRing.generate()


def open_fence() -> Fence:
    return Fence(keys())


def setup(fence: Fence) -> str:
    """Create tables and a grant letting the agent refund this user's orders. Returns the grant id."""
    c = connect(DB)
    try:
        fence.migrate(c)
        c.executescript(APP_SCHEMA)
        row = c.execute("SELECT value FROM demo_meta WHERE key = 'grant_id'").fetchone()
        if row:
            return row[0]
        orders = [r[0] for r in c.execute("SELECT id FROM orders WHERE customer = ?", (PRINCIPAL,))]
        g = fence.grant(c, principal=PRINCIPAL, operation="refund.issue", resources=orders)
        c.execute("INSERT INTO demo_meta VALUES ('grant_id', ?)", (g.grant_id,))
        return g.grant_id
    finally:
        c.close()


def grant_id() -> str:
    c = connect(DB)
    try:
        row = c.execute("SELECT value FROM demo_meta WHERE key = 'grant_id'").fetchone()
        if row is None:
            raise RuntimeError("run `python admin.py setup` first")
        return row[0]
    finally:
        c.close()


def owns_order(conn, claims, resource) -> bool:
    """Application predicate, checked inside the same transaction as the effect."""
    row = conn.execute("SELECT customer FROM orders WHERE id = ?", (resource,)).fetchone()
    return row is not None and row[0] == claims.principal


def commit_refund(fence: Fence, token: str, task_id: str, order_id: str, amount_cents: int) -> None:
    """The protected effect. Raises Denied / Duplicate / Retry; on success the refund is committed."""
    c = connect(DB)
    try:
        with fence.transaction(c):
            fence.guard(c, token, operation="refund.issue", resource=order_id,
                        operation_id=f"{task_id}:refund", predicate=owns_order)
            c.execute("UPDATE orders SET refunded_cents = refunded_cents + ? WHERE id = ?", (amount_cents, order_id))
            fence.enqueue(c, f"{task_id}:refund", "payments.refund", {"order": order_id, "amount_cents": amount_cents})
    finally:
        c.close()


def set_status(task_id: str, status: str, code: str | None = None) -> None:
    c = connect(DB)
    try:
        c.execute("UPDATE refund_tasks SET status = ?, code = ? WHERE task_id = ?", (status, code, task_id))
    finally:
        c.close()
