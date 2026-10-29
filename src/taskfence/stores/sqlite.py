"""SQLite store (WAL mode), the construction evaluated in the TaskFence paper.

Every TaskFence write transaction increments `taskfence_clock.seq` inside the
transaction. SQLite serializes writers, so `seq` is the authoritative commit
order; the auditor relies on it.

The guard's first statement is a conditional UPDATE on the grant's fence row.
It takes the database write lock and checks the fence in one step, so a
revocation either commits before it (and the check fails) or waits until the
guarded transaction ends.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..outcomes import Code, Duplicate, Retry, UsageError

SCHEMA = """
CREATE TABLE IF NOT EXISTS taskfence_clock (id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL);
INSERT OR IGNORE INTO taskfence_clock VALUES (1, 0);
CREATE TABLE IF NOT EXISTS taskfence_fences (
    grant_id    TEXT PRIMARY KEY,
    principal   TEXT NOT NULL,
    operation   TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('embedded', 'federated')),
    epoch       INTEGER,                 -- highest issued epoch; NULL in federated mode
    min_epoch   INTEGER NOT NULL,        -- tokens below this are dead; only ever increases
    guard_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS taskfence_scope (
    grant_id TEXT NOT NULL, resource TEXT NOT NULL, PRIMARY KEY (grant_id, resource));
CREATE TABLE IF NOT EXISTS taskfence_fence_log (
    seq INTEGER PRIMARY KEY, grant_id TEXT NOT NULL, kind TEXT NOT NULL,
    epoch INTEGER, min_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS taskfence_ops (
    operation_id TEXT PRIMARY KEY,
    state        TEXT NOT NULL CHECK (state IN ('COMMITTED', 'CANCELLED')),
    task_id      TEXT NOT NULL,
    grant_id     TEXT,
    epoch        INTEGER,
    resource     TEXT,
    seq          INTEGER NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS taskfence_cancelled_tasks (task_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS taskfence_receipts (
    msg_id TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL, epoch INTEGER NOT NULL,
    applied INTEGER NOT NULL, min_epoch INTEGER NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS taskfence_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES taskfence_ops(operation_id),
    topic TEXT NOT NULL, payload TEXT NOT NULL, delivered_at TEXT);
"""


def connect(path: str | Path, *, timeout_s: float = 10.0) -> sqlite3.Connection:
    """Open a connection configured the way TaskFence expects.

    WAL mode, explicit transactions (`isolation_level=None`), row access by name.
    Use one connection per thread.
    """
    c = sqlite3.connect(str(path), timeout=timeout_s, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _transient(e: sqlite3.OperationalError) -> bool:
    m = str(e).lower()
    return "locked" in m or "busy" in m


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.OperationalError as e:
        if _transient(e):
            raise Retry(str(e)) from e
        raise


class SQLiteStore:
    name = "sqlite"

    @staticmethod
    def errors():
        return translate_errors()

    # ---------------------------------------------------------------- transactions

    def migrate(self, conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            raise UsageError("migrate() must not run inside an open transaction")
        conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[None]:
        """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
        if getattr(conn, "autocommit", None) is False:      # Python 3.12+ PEP 249 mode
            try:
                yield
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return
        if conn.in_transaction:
            raise UsageError("a transaction is already open on this connection")
        with translate_errors():
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            with translate_errors():
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    @contextmanager
    def owned_or_joined(self, conn: sqlite3.Connection) -> Iterator[bool]:
        """Join the caller's transaction if one is open; otherwise own one. Yields True if owned."""
        if conn.in_transaction and getattr(conn, "autocommit", None) is not False:
            yield False
        else:
            with self.transaction(conn):
                yield True

    @staticmethod
    def require_transaction(conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise UsageError("guard() must run inside the transaction that commits the effect; "
                             "use fence.transaction(conn)")

    @staticmethod
    def bump(conn: sqlite3.Connection) -> int:
        return conn.execute("UPDATE taskfence_clock SET seq = seq + 1 WHERE id = 1 RETURNING seq").fetchone()[0]

    def _log(self, conn: sqlite3.Connection, grant_id: str, kind: str, epoch: int | None, min_epoch: int) -> int:
        seq = self.bump(conn)
        conn.execute("INSERT INTO taskfence_fence_log VALUES (?, ?, ?, ?, ?)", (seq, grant_id, kind, epoch, min_epoch))
        return seq

    # ---------------------------------------------------------------- reads

    @staticmethod
    def fence_row(conn: sqlite3.Connection, grant_id: str) -> dict[str, Any] | None:
        r = conn.execute("SELECT grant_id, principal, operation, mode, epoch, min_epoch FROM taskfence_fences"
                         " WHERE grant_id = ?", (grant_id,)).fetchone()
        return None if r is None else dict(zip(("grant_id", "principal", "operation", "mode", "epoch",
                                                "min_epoch"), tuple(r)))

    @staticmethod
    def scope(conn: sqlite3.Connection, grant_id: str) -> tuple[str, ...]:
        rows = conn.execute("SELECT resource FROM taskfence_scope WHERE grant_id = ? ORDER BY resource",
                            (grant_id,)).fetchall()
        return tuple(r[0] for r in rows)

    # ---------------------------------------------------------------- embedded admin

    def create_grant(self, conn, grant_id: str, principal: str, operation: str, resources: list[str]) -> int:
        conn.execute("INSERT INTO taskfence_fences (grant_id, principal, operation, mode, epoch, min_epoch)"
                     " VALUES (?, ?, ?, 'embedded', 1, 1)", (grant_id, principal, operation))
        conn.executemany("INSERT INTO taskfence_scope VALUES (?, ?)", [(grant_id, r) for r in resources])
        return self._log(conn, grant_id, "GRANT", 1, 1)

    def revoke(self, conn, grant_id: str) -> tuple[int, int]:
        e, floor = conn.execute("UPDATE taskfence_fences SET min_epoch = max(min_epoch, epoch + 1)"
                                " WHERE grant_id = ? RETURNING epoch, min_epoch", (grant_id,)).fetchone()
        return floor, self._log(conn, grant_id, "REVOKE", e, floor)

    def regrant(self, conn, grant_id: str, resources: list[str] | None) -> tuple[int, int]:
        new_epoch = conn.execute("UPDATE taskfence_fences SET epoch = max(epoch, min_epoch - 1) + 1,"
                                 " min_epoch = max(epoch, min_epoch - 1) + 1 WHERE grant_id = ? RETURNING epoch",
                                 (grant_id,)).fetchone()[0]
        if resources is not None:
            self._replace_scope(conn, grant_id, resources)
        return new_epoch, self._log(conn, grant_id, "REGRANT", new_epoch, new_epoch)

    @staticmethod
    def _replace_scope(conn, grant_id: str, resources: list[str]) -> None:
        conn.execute("DELETE FROM taskfence_scope WHERE grant_id = ?", (grant_id,))
        conn.executemany("INSERT INTO taskfence_scope VALUES (?, ?)", [(grant_id, r) for r in resources])

    def cancel_task(self, conn, task_id: str) -> int:
        seq = self.bump(conn)
        conn.execute("INSERT OR IGNORE INTO taskfence_cancelled_tasks VALUES (?, ?)", (task_id, seq))
        return seq

    def cancel_operation(self, conn, operation_id: str, task_id: str) -> bool:
        """Tombstone an operation that has not committed. Returns False if it already committed."""
        prior = conn.execute("SELECT state FROM taskfence_ops WHERE operation_id = ?", (operation_id,)).fetchone()
        if prior is not None:
            return prior[0] == "CANCELLED"
        seq = self.bump(conn)
        conn.execute("INSERT INTO taskfence_ops (operation_id, state, task_id, seq) VALUES (?, 'CANCELLED', ?, ?)",
                     (operation_id, task_id, seq))
        return True

    # ---------------------------------------------------------------- guard steps

    def lock_and_check_fence(self, conn, grant_id: str, principal: str, operation: str, epoch: int) -> Code | None:
        """Take the write lock via the fence row and check the epoch. None means the fence admits the token."""
        cur = conn.execute(
            "UPDATE taskfence_fences SET guard_count = guard_count + 1"
            " WHERE grant_id = :g AND principal = :p AND operation = :op"
            " AND :e >= min_epoch AND (epoch IS NULL OR :e <= epoch)",
            {"g": grant_id, "p": principal, "op": operation, "e": epoch})
        if cur.rowcount == 1:
            # Federated mode: an authentic newer epoch proves a regrant, so raise the floor (D5 semantics).
            adv = conn.execute("UPDATE taskfence_fences SET min_epoch = ? WHERE grant_id = ? AND mode = 'federated'"
                               " AND min_epoch < ?", (epoch, grant_id, epoch))
            if adv.rowcount == 1:
                self._log(conn, grant_id, "ADVANCE", epoch, epoch)
            return None
        return self._diagnose(conn, grant_id, principal, operation, epoch)

    def _diagnose(self, conn, grant_id: str, principal: str, operation: str, epoch: int) -> Code:
        f = self.fence_row(conn, grant_id)
        if f is None:
            return Code.NO_GRANT
        if principal != f["principal"]:
            return Code.PRINCIPAL_MISMATCH
        if operation != f["operation"]:
            return Code.OPERATION_MISMATCH
        if epoch < f["min_epoch"]:
            return Code.STALE_EPOCH
        return Code.FUTURE_EPOCH

    @staticmethod
    def in_scope(conn, grant_id: str, resource: str | None) -> bool:
        return conn.execute(
            "SELECT 1 FROM taskfence_scope WHERE grant_id = ? AND (resource = '*' OR resource = ?)",
            (grant_id, resource)).fetchone() is not None

    @staticmethod
    def task_cancelled(conn, task_id: str) -> bool:
        return conn.execute("SELECT 1 FROM taskfence_cancelled_tasks WHERE task_id = ?",
                            (task_id,)).fetchone() is not None

    def insert_op(self, conn, operation_id: str, task_id: str, grant_id: str, epoch: int,
                  resource: str | None) -> int | Code:
        """Returns the commit seq, or Code.OPERATION_CANCELLED. Raises Duplicate."""
        prior = conn.execute("SELECT state FROM taskfence_ops WHERE operation_id = ?", (operation_id,)).fetchone()
        if prior is not None:
            if prior[0] == "CANCELLED":
                return Code.OPERATION_CANCELLED
            raise Duplicate(operation_id)
        seq = self.bump(conn)
        try:
            conn.execute("INSERT INTO taskfence_ops (operation_id, state, task_id, grant_id, epoch, resource, seq)"
                         " VALUES (?, 'COMMITTED', ?, ?, ?, ?, ?)",
                         (operation_id, task_id, grant_id, epoch, resource, seq))
        except sqlite3.IntegrityError:
            raise Duplicate(operation_id) from None
        return seq

    # ---------------------------------------------------------------- federated owner side

    def apply_message(self, conn, msg: dict[str, Any]) -> dict[str, Any]:
        """Apply INSTALL or FENCE idempotently (by msg_id). The floor is max(floor, epoch)."""
        prior = conn.execute("SELECT * FROM taskfence_receipts WHERE msg_id = ?", (msg["msg_id"],)).fetchone()
        if prior is not None:
            return {**dict(zip(("msg_id", "kind", "grant_id", "epoch", "applied", "min_epoch", "seq"), tuple(prior))),
                    "duplicate": True}
        kind, g = msg["kind"], msg["grant_id"]
        e = int(msg["epoch"] if kind == "INSTALL" else msg["fence_epoch"])
        cur = self.fence_row(conn, g)
        if cur is not None and cur["mode"] != "federated":
            raise UsageError(f"grant {g!r} is an embedded grant; federated messages cannot change it")
        applied = cur is None or e > cur["min_epoch"]
        if cur is None:
            conn.execute("INSERT INTO taskfence_fences (grant_id, principal, operation, mode, epoch, min_epoch)"
                         " VALUES (?, ?, ?, 'federated', NULL, ?)", (g, msg["principal"], msg["operation"], e))
        elif applied:
            conn.execute("UPDATE taskfence_fences SET min_epoch = ? WHERE grant_id = ?", (e, g))
        if kind == "INSTALL" and applied:
            self._replace_scope(conn, g, list(msg.get("resources", [])))
        floor = self.fence_row(conn, g)["min_epoch"]
        seq = self._log(conn, g, kind if applied else f"{kind}_NOOP", e, floor)
        conn.execute("INSERT INTO taskfence_receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (msg["msg_id"], kind, g, e, int(applied), floor, seq))
        return {"msg_id": msg["msg_id"], "kind": kind, "grant_id": g, "epoch": e, "applied": int(applied),
                "min_epoch": floor, "seq": seq, "duplicate": False}

    # ---------------------------------------------------------------- outbox

    @staticmethod
    def outbox_enqueue(conn, operation_id: str, topic: str, payload: Any) -> int:
        r = conn.execute("SELECT state FROM taskfence_ops WHERE operation_id = ?", (operation_id,)).fetchone()
        if r is None or r[0] != "COMMITTED":
            raise UsageError("enqueue only after guard() admitted this operation_id, in the same transaction")
        return conn.execute("INSERT INTO taskfence_outbox (operation_id, topic, payload) VALUES (?, ?, ?)",
                            (operation_id, topic, json.dumps(payload))).lastrowid

    @staticmethod
    def outbox_pending(conn, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT id, operation_id, topic, payload FROM taskfence_outbox"
                            " WHERE delivered_at IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "operation_id": r[1], "topic": r[2], "payload": json.loads(r[3])} for r in rows]

    @staticmethod
    def outbox_mark_delivered(conn, entry_id: int) -> None:
        conn.execute("UPDATE taskfence_outbox SET delivered_at = CURRENT_TIMESTAMP WHERE id = ?", (entry_id,))
