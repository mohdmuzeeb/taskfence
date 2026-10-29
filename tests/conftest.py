"""Every test runs on each back-end: sqlite, pg-shared and pg-exclusive.

PostgreSQL tests need TASKFENCE_PG_DSN (a server with track_commit_timestamp = on);
they are skipped otherwise. Each test gets its own schema.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

import pytest

from taskfence import Admission, Fence, KeyRing
from taskfence.stores.sqlite import SQLiteStore, connect as sqlite_connect
from taskfence.testing import hook

PG_DSN = os.environ.get("TASKFENCE_PG_DSN")
BACKENDS = ["sqlite", "pg-shared", "pg-exclusive"]

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer TEXT NOT NULL, refunds INTEGER NOT NULL DEFAULT 0);
INSERT INTO orders VALUES ('order:1', 'user:42', 0), ('order:2', 'user:42', 0), ('order:3', 'user:7', 0)
    ON CONFLICT (id) DO NOTHING;
"""


def is_pg(conn) -> bool:
    return type(conn).__module__.startswith("psycopg")


def q(conn, sql: str) -> str:
    """Write test SQL with `?` placeholders; converted for psycopg."""
    return sql.replace("?", "%s") if is_pg(conn) else sql


def begin(conn) -> None:
    conn.execute("BEGIN" if is_pg(conn) else "BEGIN IMMEDIATE")


def in_txn(conn) -> bool:
    if is_pg(conn):
        from psycopg.pq import TransactionStatus
        return conn.info.transaction_status != TransactionStatus.IDLE
    return conn.in_transaction


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    if request.param.startswith("pg") and not PG_DSN:
        pytest.skip("set TASKFENCE_PG_DSN to run PostgreSQL tests")
    return request.param


@pytest.fixture
def connect_db(backend, tmp_path) -> Callable:
    opened = []
    if backend == "sqlite":
        def _connect(timeout_s: float = 10.0):
            c = sqlite_connect(tmp_path / "app.sqlite", timeout_s=timeout_s)
            opened.append(c)
            return c
        yield _connect
    else:
        import psycopg
        from taskfence.stores.postgres import connect as pg_connect
        schema = "tf_" + secrets.token_hex(6)
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")

        def _connect(timeout_s: float = 10.0):
            c = pg_connect(PG_DSN, options=f"-c search_path={schema} -c lock_timeout={int(timeout_s * 1000)}")
            opened.append(c)
            return c
        yield _connect
        for c in opened:
            c.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
    for c in opened:
        c.close()


@pytest.fixture
def fence(backend) -> Fence:
    if backend == "sqlite":
        return Fence(KeyRing.generate(), SQLiteStore())
    from taskfence.stores.postgres import PostgresStore
    return Fence(KeyRing.generate(), PostgresStore(lock=backend.removeprefix("pg-")))


@pytest.fixture
def conn(connect_db, fence):
    c = connect_db()
    fence.migrate(c)
    if is_pg(c):
        c.execute(APP_SCHEMA)
    else:
        c.executescript(APP_SCHEMA)
    return c


@pytest.fixture
def grant(conn, fence):
    return fence.grant(conn, principal="user:42", operation="refund.issue", resources=["order:1", "order:2"])


def refund(fence: Fence, conn, token: str, order: str = "order:1", operation_id: str | None = None) -> Admission:
    """The application's protected effect: guard + UPDATE in one transaction."""
    hook("app.before_txn", task_id=fence.verify(token).task_id)   # slow work happens before this point
    with fence.transaction(conn):
        adm = fence.guard(conn, token, operation="refund.issue", resource=order,
                          operation_id=operation_id or f"{fence.verify(token).task_id}:refund:{order}")
        conn.execute(q(conn, "UPDATE orders SET refunds = refunds + 1 WHERE id = ?"), (order,))
    return adm


def refunds(conn, order: str = "order:1") -> int:
    return conn.execute(q(conn, "SELECT refunds FROM orders WHERE id = ?"), (order,)).fetchone()[0]
