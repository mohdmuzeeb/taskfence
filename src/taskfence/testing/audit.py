"""Independent auditor: judges the final database with SQL, not with guard code.

A violation is a committed operation whose commit position (`seq`) comes after
a fence-log entry that had already raised the grant's floor above the
operation's epoch, or after its task was cancelled.
"""

from __future__ import annotations

from typing import Any

POST_REVOCATION = """
SELECT o.operation_id, o.task_id, o.grant_id, o.epoch, o.seq AS commit_seq,
       MIN(l.seq) AS fence_seq, 'POST_REVOCATION' AS kind
FROM taskfence_ops o
JOIN taskfence_fence_log l
  ON l.grant_id = o.grant_id AND l.seq < o.seq AND l.min_epoch > o.epoch
WHERE o.state = 'COMMITTED'
GROUP BY o.operation_id
"""

POST_CANCELLATION = """
SELECT o.operation_id, o.task_id, o.grant_id, o.epoch, o.seq AS commit_seq,
       c.seq AS fence_seq, 'POST_CANCELLATION' AS kind
FROM taskfence_ops o
JOIN taskfence_cancelled_tasks c ON c.task_id = o.task_id AND c.seq < o.seq
WHERE o.state = 'COMMITTED'
"""


# PostgreSQL: `seq` comes from a sequence and is not commit order, so compare the
# commit timestamps of the rows' inserting transactions instead.
PG_POST_REVOCATION = """
SELECT o.operation_id, o.task_id, o.grant_id, o.epoch, o.seq AS commit_seq,
       MIN(l.seq) AS fence_seq, 'POST_REVOCATION' AS kind
FROM taskfence_ops o
JOIN taskfence_fence_log l
  ON l.grant_id = o.grant_id AND l.min_epoch > o.epoch
 AND pg_xact_commit_timestamp(l.xmin) < pg_xact_commit_timestamp(o.xmin)
WHERE o.state = 'COMMITTED'
GROUP BY o.operation_id, o.task_id, o.grant_id, o.epoch, o.seq
"""

PG_POST_CANCELLATION = """
SELECT o.operation_id, o.task_id, o.grant_id, o.epoch, o.seq AS commit_seq,
       MIN(c.seq) AS fence_seq, 'POST_CANCELLATION' AS kind
FROM taskfence_ops o
JOIN taskfence_cancel_log c
  ON c.task_id = o.task_id AND pg_xact_commit_timestamp(c.xmin) < pg_xact_commit_timestamp(o.xmin)
WHERE o.state = 'COMMITTED'
GROUP BY o.operation_id, o.task_id, o.grant_id, o.epoch, o.seq
"""


def _is_postgres(conn) -> bool:
    return type(conn).__module__.startswith("psycopg")


def audit(conn) -> list[dict[str, Any]]:
    """Return every violation of the safety property found in the database (empty list = clean).

    On PostgreSQL this needs `track_commit_timestamp = on`.
    """
    cols = ("operation_id", "task_id", "grant_id", "epoch", "commit_seq", "fence_seq", "kind")
    queries = (POST_REVOCATION, POST_CANCELLATION)
    if _is_postgres(conn):
        if conn.execute("SHOW track_commit_timestamp").fetchone()[0] != "on":
            raise RuntimeError("the PostgreSQL auditor needs track_commit_timestamp = on (server setting)")
        queries = (PG_POST_REVOCATION, PG_POST_CANCELLATION)
    out: list[dict[str, Any]] = []
    for q in queries:
        out.extend(dict(zip(cols, tuple(r))) for r in conn.execute(q).fetchall())
    return sorted(out, key=lambda v: v["commit_seq"])


def assert_no_violations(conn) -> None:
    v = audit(conn)
    if v:
        lines = "\n".join(f"  {x['kind']}: op={x['operation_id']} epoch={x['epoch']} "
                          f"committed at seq {x['commit_seq']} after fence seq {x['fence_seq']}" for x in v)
        raise AssertionError(f"{len(v)} TaskFence safety violation(s):\n{lines}")
