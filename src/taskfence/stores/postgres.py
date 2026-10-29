"""PostgreSQL store (psycopg 3). Not part of the paper's evaluation; see docs/DESIGN.md §6.

Locking, per grant:

* ``lock="shared"`` (default). A guard takes a *shared* transaction-level
  advisory lock on the grant, then reads the fence row ``FOR SHARE``. Revoke and
  regrant take the *exclusive* advisory lock, then UPDATE the fence row. Guards
  for one grant run in parallel. Advisory locks queue fairly, so a steady stream
  of guards cannot starve a revocation (plain ``FOR SHARE`` row locks can).
* ``lock="exclusive"``. A guard reads the fence row ``FOR UPDATE``. Guards for
  one grant serialize, as in D5.

Either way, a revocation waits for in-flight guarded transactions of that grant,
and any guard that locks after the revocation commits reads the new floor. Under
READ COMMITTED each statement takes a fresh snapshot after the lock is granted.
Under REPEATABLE READ and SERIALIZABLE, locking a row changed after the snapshot
raises a serialization failure, which surfaces as ``Retry``.

Cancellation: the guard upserts the task's row in ``taskfence_tasks`` (row lock
held until commit) and reads its ``cancelled`` flag. ``cancel_task`` upserts the
same row, so the two serialize.

``seq`` comes from a sequence. It is unique but is not commit order. The auditor
uses commit timestamps instead (requires ``track_commit_timestamp = on``).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Literal

try:
    import psycopg
    from psycopg import errors as pgerr
    from psycopg.pq import TransactionStatus
except ImportError as e:            # pragma: no cover
    raise ImportError("PostgresStore needs psycopg 3: pip install 'taskfence[postgres]'") from e

from ..outcomes import Code, Duplicate, Retry, UsageError

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS taskfence_seq;
CREATE TABLE IF NOT EXISTS taskfence_fences (
    grant_id    text PRIMARY KEY,
    principal   text   NOT NULL,
    operation   text   NOT NULL,
    mode        text   NOT NULL CHECK (mode IN ('embedded', 'federated')),
    epoch       bigint,                  -- highest issued epoch; NULL in federated mode
    min_epoch   bigint NOT NULL,         -- tokens below this are dead; only ever increases
    updated_at  timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS taskfence_scope (
    grant_id text NOT NULL REFERENCES taskfence_fences ON DELETE CASCADE,
    resource text NOT NULL, PRIMARY KEY (grant_id, resource));
CREATE TABLE IF NOT EXISTS taskfence_fence_log (
    seq bigint PRIMARY KEY, grant_id text NOT NULL, kind text NOT NULL,
    epoch bigint, min_epoch bigint NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS taskfence_ops (
    operation_id text PRIMARY KEY,
    state        text NOT NULL CHECK (state IN ('COMMITTED', 'CANCELLED')),
    task_id      text NOT NULL,
    grant_id     text,
    epoch        bigint,
    resource     text,
    seq          bigint NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS taskfence_tasks (
    task_id text PRIMARY KEY, cancelled boolean NOT NULL DEFAULT false, cancelled_seq bigint);
CREATE TABLE IF NOT EXISTS taskfence_cancel_log (    -- append-only; the auditor reads its commit timestamps
    seq bigint PRIMARY KEY, task_id text NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS taskfence_receipts (
    msg_id text PRIMARY KEY, kind text NOT NULL, grant_id text NOT NULL, epoch bigint NOT NULL,
    applied boolean NOT NULL, min_epoch bigint NOT NULL, seq bigint NOT NULL);
CREATE TABLE IF NOT EXISTS taskfence_outbox (
    id bigserial PRIMARY KEY,
    operation_id text NOT NULL REFERENCES taskfence_ops(operation_id),
    topic text NOT NULL, payload jsonb NOT NULL, delivered_at timestamptz);
"""

_RETRYABLE = (pgerr.SerializationFailure, pgerr.DeadlockDetected, pgerr.LockNotAvailable,
              pgerr.QueryCanceled)
# Advisory-lock namespace: hashtext('taskfence') would also do; a constant keeps it obvious.
_LOCK_CLASS = 0x7446          # "tF"


def connect(dsn: str, **kwargs: Any) -> "psycopg.Connection":
    """Open a connection configured the way TaskFence expects (autocommit; explicit transactions)."""
    return psycopg.connect(dsn, autocommit=True, **kwargs)


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except _RETRYABLE as e:
        raise Retry(f"{type(e).__name__}: {e}") from e


def _status(conn) -> TransactionStatus:
    return conn.info.transaction_status


class PostgresStore:
    name = "postgres"

    def __init__(self, lock: Literal["shared", "exclusive"] = "shared"):
        if lock not in ("shared", "exclusive"):
            raise ValueError("lock must be 'shared' or 'exclusive'")
        self.lock = lock

    @staticmethod
    def errors():
        return translate_errors()

    # ---------------------------------------------------------------- transactions

    def migrate(self, conn) -> None:
        if _status(conn) != TransactionStatus.IDLE:
            raise UsageError("migrate() must not run inside an open transaction")
        with conn.transaction():
            conn.execute(SCHEMA)

    @contextmanager
    def transaction(self, conn) -> Iterator[None]:
        if _status(conn) != TransactionStatus.IDLE:
            raise UsageError("a transaction is already open on this connection")
        with translate_errors(), conn.transaction():
            yield

    @contextmanager
    def owned_or_joined(self, conn) -> Iterator[bool]:
        if _status(conn) == TransactionStatus.INTRANS:
            yield False
        else:
            with self.transaction(conn):
                yield True

    @staticmethod
    def require_transaction(conn) -> None:
        if _status(conn) != TransactionStatus.INTRANS:
            raise UsageError("guard() must run inside the transaction that commits the effect; "
                             "use fence.transaction(conn)")

    @staticmethod
    def bump(conn) -> int:
        return conn.execute("SELECT nextval('taskfence_seq')").fetchone()[0]

    def _log(self, conn, grant_id: str, kind: str, epoch: int | None, min_epoch: int) -> int:
        seq = self.bump(conn)
        conn.execute("INSERT INTO taskfence_fence_log (seq, grant_id, kind, epoch, min_epoch)"
                     " VALUES (%s, %s, %s, %s, %s)", (seq, grant_id, kind, epoch, min_epoch))
        return seq

    @staticmethod
    def _grant_lock(conn, grant_id: str, *, exclusive: bool) -> None:
        fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        conn.execute(f"SELECT {fn}(%s, hashtext(%s))", (_LOCK_CLASS, grant_id))

    def _writer_lock(self, conn, grant_id: str) -> None:
        """Taken by every control-plane change to a fence row."""
        if self.lock == "shared":
            self._grant_lock(conn, grant_id, exclusive=True)

    # ---------------------------------------------------------------- reads

    @staticmethod
    def fence_row(conn, grant_id: str, *, lock: str = "") -> dict[str, Any] | None:
        r = conn.execute("SELECT grant_id, principal, operation, mode, epoch, min_epoch FROM taskfence_fences"
                         f" WHERE grant_id = %s {lock}", (grant_id,)).fetchone()
        return None if r is None else dict(zip(("grant_id", "principal", "operation", "mode", "epoch",
                                                "min_epoch"), r))

    @staticmethod
    def scope(conn, grant_id: str) -> tuple[str, ...]:
        rows = conn.execute("SELECT resource FROM taskfence_scope WHERE grant_id = %s ORDER BY resource",
                            (grant_id,)).fetchall()
        return tuple(r[0] for r in rows)

    # ---------------------------------------------------------------- embedded admin

    def create_grant(self, conn, grant_id: str, principal: str, operation: str, resources: list[str]) -> int:
        conn.execute("INSERT INTO taskfence_fences (grant_id, principal, operation, mode, epoch, min_epoch)"
                     " VALUES (%s, %s, %s, 'embedded', 1, 1)", (grant_id, principal, operation))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO taskfence_scope VALUES (%s, %s)", [(grant_id, r) for r in resources])
        return self._log(conn, grant_id, "GRANT", 1, 1)

    def revoke(self, conn, grant_id: str) -> tuple[int, int]:
        self._writer_lock(conn, grant_id)
        cur = conn.execute("UPDATE taskfence_fences SET min_epoch = GREATEST(min_epoch, epoch + 1),"
                           " updated_at = now() WHERE grant_id = %s RETURNING epoch, min_epoch", (grant_id,))
        e, floor = cur.fetchone()
        return floor, self._log(conn, grant_id, "REVOKE", e, floor)

    def regrant(self, conn, grant_id: str, resources: list[str] | None) -> tuple[int, int]:
        self._writer_lock(conn, grant_id)
        new_epoch = conn.execute("UPDATE taskfence_fences SET epoch = GREATEST(epoch, min_epoch - 1) + 1,"
                                 " min_epoch = GREATEST(epoch, min_epoch - 1) + 1, updated_at = now()"
                                 " WHERE grant_id = %s RETURNING epoch", (grant_id,)).fetchone()[0]
        if resources is not None:
            self._replace_scope(conn, grant_id, resources)
        return new_epoch, self._log(conn, grant_id, "REGRANT", new_epoch, new_epoch)

    @staticmethod
    def _replace_scope(conn, grant_id: str, resources: list[str]) -> None:
        conn.execute("DELETE FROM taskfence_scope WHERE grant_id = %s", (grant_id,))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO taskfence_scope VALUES (%s, %s)", [(grant_id, r) for r in resources])

    def cancel_task(self, conn, task_id: str) -> int:
        # Lock first, then take seq, so seq order matches commit order against conflicting guards.
        conn.execute("INSERT INTO taskfence_tasks (task_id, cancelled) VALUES (%s, true)"
                     " ON CONFLICT (task_id) DO UPDATE SET cancelled = true WHERE NOT taskfence_tasks.cancelled",
                     (task_id,))
        seq = self.bump(conn)
        conn.execute("UPDATE taskfence_tasks SET cancelled_seq = COALESCE(cancelled_seq, %s) WHERE task_id = %s",
                     (seq, task_id))
        conn.execute("INSERT INTO taskfence_cancel_log (seq, task_id) VALUES (%s, %s)", (seq, task_id))
        return seq

    def cancel_operation(self, conn, operation_id: str, task_id: str) -> bool:
        seq = self.bump(conn)
        r = conn.execute("INSERT INTO taskfence_ops (operation_id, state, task_id, seq)"
                         " VALUES (%s, 'CANCELLED', %s, %s) ON CONFLICT (operation_id) DO NOTHING RETURNING 1",
                         (operation_id, task_id, seq)).fetchone()
        if r is not None:
            return True
        prior = conn.execute("SELECT state FROM taskfence_ops WHERE operation_id = %s", (operation_id,)).fetchone()
        return prior is not None and prior[0] == "CANCELLED"

    # ---------------------------------------------------------------- guard steps

    def lock_and_check_fence(self, conn, grant_id: str, principal: str, operation: str, epoch: int) -> Code | None:
        if self.lock == "shared":
            self._grant_lock(conn, grant_id, exclusive=False)
        # Federated: an authentic newer epoch proves a regrant, so raise the floor (D5 semantics).
        adv = conn.execute("UPDATE taskfence_fences SET min_epoch = %s, updated_at = now()"
                           " WHERE grant_id = %s AND mode = 'federated' AND principal = %s AND operation = %s"
                           " AND min_epoch < %s", (epoch, grant_id, principal, operation, epoch))
        if adv.rowcount == 1:
            self._log(conn, grant_id, "ADVANCE", epoch, epoch)
        f = self.fence_row(conn, grant_id, lock="FOR SHARE" if self.lock == "shared" else "FOR UPDATE")
        if f is None:
            return Code.NO_GRANT
        if principal != f["principal"]:
            return Code.PRINCIPAL_MISMATCH
        if operation != f["operation"]:
            return Code.OPERATION_MISMATCH
        if epoch < f["min_epoch"]:
            return Code.STALE_EPOCH
        if f["epoch"] is not None and epoch > f["epoch"]:
            return Code.FUTURE_EPOCH
        return None

    @staticmethod
    def in_scope(conn, grant_id: str, resource: str | None) -> bool:
        return conn.execute(
            "SELECT 1 FROM taskfence_scope WHERE grant_id = %s AND (resource = '*' OR resource = %s)",
            (grant_id, resource)).fetchone() is not None

    @staticmethod
    def task_cancelled(conn, task_id: str) -> bool:
        # The row lock is held until commit, so cancel_task() (which writes this row) serializes with us.
        conn.execute("INSERT INTO taskfence_tasks (task_id) VALUES (%s) ON CONFLICT (task_id) DO NOTHING", (task_id,))
        return conn.execute("SELECT cancelled FROM taskfence_tasks WHERE task_id = %s FOR SHARE",
                            (task_id,)).fetchone()[0]

    def insert_op(self, conn, operation_id: str, task_id: str, grant_id: str, epoch: int,
                  resource: str | None) -> int | Code:
        seq = self.bump(conn)
        r = conn.execute("INSERT INTO taskfence_ops (operation_id, state, task_id, grant_id, epoch, resource, seq)"
                         " VALUES (%s, 'COMMITTED', %s, %s, %s, %s, %s) ON CONFLICT (operation_id) DO NOTHING"
                         " RETURNING seq", (operation_id, task_id, grant_id, epoch, resource, seq)).fetchone()
        if r is not None:
            return seq
        prior = conn.execute("SELECT state FROM taskfence_ops WHERE operation_id = %s", (operation_id,)).fetchone()
        if prior is not None and prior[0] == "CANCELLED":
            return Code.OPERATION_CANCELLED
        raise Duplicate(operation_id)

    # ---------------------------------------------------------------- federated owner side

    def apply_message(self, conn, msg: dict[str, Any]) -> dict[str, Any]:
        cols = ("msg_id", "kind", "grant_id", "epoch", "applied", "min_epoch", "seq")
        kind, g = msg["kind"], msg["grant_id"]
        self._writer_lock(conn, g)
        prior = conn.execute(f"SELECT {', '.join(cols)} FROM taskfence_receipts WHERE msg_id = %s",
                             (msg["msg_id"],)).fetchone()
        if prior is not None:
            return {**dict(zip(cols, prior)), "duplicate": True}
        e = int(msg["epoch"] if kind == "INSTALL" else msg["fence_epoch"])
        cur = self.fence_row(conn, g, lock="FOR UPDATE")
        if cur is not None and cur["mode"] != "federated":
            raise UsageError(f"grant {g!r} is an embedded grant; federated messages cannot change it")
        applied = cur is None or e > cur["min_epoch"]
        if cur is None:
            conn.execute("INSERT INTO taskfence_fences (grant_id, principal, operation, mode, epoch, min_epoch)"
                         " VALUES (%s, %s, %s, 'federated', NULL, %s)", (g, msg["principal"], msg["operation"], e))
        elif applied:
            conn.execute("UPDATE taskfence_fences SET min_epoch = %s, updated_at = now() WHERE grant_id = %s", (e, g))
        if kind == "INSTALL" and applied:
            self._replace_scope(conn, g, list(msg.get("resources", [])))
        floor = self.fence_row(conn, g)["min_epoch"]
        seq = self._log(conn, g, kind if applied else f"{kind}_NOOP", e, floor)
        conn.execute("INSERT INTO taskfence_receipts VALUES (%s, %s, %s, %s, %s, %s, %s)",
                     (msg["msg_id"], kind, g, e, applied, floor, seq))
        return {"msg_id": msg["msg_id"], "kind": kind, "grant_id": g, "epoch": e, "applied": applied,
                "min_epoch": floor, "seq": seq, "duplicate": False}

    # ---------------------------------------------------------------- outbox

    @staticmethod
    def outbox_enqueue(conn, operation_id: str, topic: str, payload: Any) -> int:
        r = conn.execute("SELECT state FROM taskfence_ops WHERE operation_id = %s", (operation_id,)).fetchone()
        if r is None or r[0] != "COMMITTED":
            raise UsageError("enqueue only after guard() admitted this operation_id, in the same transaction")
        return conn.execute("INSERT INTO taskfence_outbox (operation_id, topic, payload) VALUES (%s, %s, %s)"
                            " RETURNING id", (operation_id, topic, json.dumps(payload))).fetchone()[0]

    @staticmethod
    def outbox_pending(conn, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT id, operation_id, topic, payload FROM taskfence_outbox"
                            " WHERE delivered_at IS NULL ORDER BY id LIMIT %s", (limit,)).fetchall()
        return [{"id": r[0], "operation_id": r[1], "topic": r[2], "payload": r[3]} for r in rows]

    @staticmethod
    def outbox_mark_delivered(conn, entry_id: int) -> None:
        conn.execute("UPDATE taskfence_outbox SET delivered_at = now() WHERE id = %s", (entry_id,))
