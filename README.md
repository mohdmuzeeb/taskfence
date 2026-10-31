# TaskFence

[![CI](https://github.com/mohdmuzeeb/taskfence/actions/workflows/ci.yml/badge.svg)](https://github.com/mohdmuzeeb/taskfence/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/taskfence)](https://pypi.org/project/taskfence/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23842363.svg)](https://doi.org/10.5281/zenodo.23842363)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/mohdmuzeeb/taskfence/blob/main/LICENSE)

**Revocation-safe side effects for long-running AI agent tasks.**

If a user revokes permission while an agent task is running, TaskFence guarantees the task cannot commit its side effect afterwards. The check runs **inside your own database transaction**, at commit time, not before it.

## The problem

1. An agent task is admitted: the user has granted `refund.issue`.
2. The task works for 10 minutes (LLM calls, tool calls, waiting for input).
3. At minute 4, the user revokes the grant.
4. At minute 10, the task writes the refund. **Most servers allow this**, because they checked permission when the task started, or just before the write in a separate step.

The TaskFence paper measured this window. In one randomized run, designs that check at start or in a separate pre-commit step let 317 provable commits through after revocation. Checking inside the commit transaction let none through.

## Install

```bash
pip install taskfence                 # SQLite; core has no dependencies
```

```bash
pip install "taskfence[postgres]"     # PostgreSQL via psycopg 3
```

Requires Python 3.12+. PostgreSQL 15, 16 and 17 are tested in CI.

## Example

```python
from taskfence import Denied, Fence, KeyRing
from taskfence.stores.sqlite import connect

fence = Fence(KeyRing.from_env("TASKFENCE_KEYS"))   # or KeyRing.generate() to try it out
conn = connect("app.sqlite")
fence.migrate(conn)
conn.executescript("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, refunded INTEGER DEFAULT 0);"
                   "INSERT OR IGNORE INTO orders VALUES ('order:9001', 0);")

# The user grants the agent permission; a task is admitted with a signed token.
g = fence.grant(conn, principal="user:42", operation="refund.issue", resources=["order:9001"])
token = fence.issue(conn, g.grant_id, task_id="task-1")

# ... the task works for minutes; meanwhile the user may call fence.revoke(conn, g.grant_id) ...

try:
    with fence.transaction(conn):            # do slow work BEFORE opening the transaction
        fence.guard(conn, token, operation="refund.issue", resource="order:9001",
                    operation_id="task-1:refund")
        conn.execute("UPDATE orders SET refunded = 1 WHERE id = 'order:9001'")
except Denied as e:
    print("blocked:", e.code)                # STALE_EPOCH if access was revoked meanwhile
```

[examples/01_quickstart_sqlite.py](https://github.com/mohdmuzeeb/taskfence/blob/main/examples/01_quickstart_sqlite.py) runs this with a revocation in the middle and prints `refund blocked: STALE_EPOCH`.

For PostgreSQL:

```python
from taskfence.stores.postgres import PostgresStore, connect
fence = Fence(KeyRing.from_env(), PostgresStore())   # fair shared locking per grant (default)
```

## How it works

- **Grants have epochs.** Revoking a grant raises a fence (`min_epoch`) in your database. Regranting starts a new epoch.
- **Tasks carry a signed token** naming the epoch they were admitted under.
- **`guard()` checks the token in the same transaction as the effect.** The commit either sees the revocation, or is strictly ordered before it.
- **The same check also:**
  - fences cancelled tasks
  - makes each `operation_id` commit at most once
  - enforces resource scope and an optional application predicate
  - can write an outbox entry for external calls

Details: [docs/DESIGN.md](https://github.com/mohdmuzeeb/taskfence/blob/main/docs/DESIGN.md) and [docs/THREAT_MODEL.md](https://github.com/mohdmuzeeb/taskfence/blob/main/docs/THREAT_MODEL.md).

## What it guarantees, and what it does not

- ✅ No effect protected by `guard()` commits after revocation, or after task cancellation, has taken effect.
- ✅ A revocation waits for at most the guarded transactions already in flight, never for a whole task.
- ❌ Not retroactive: effects that committed before the revocation stay committed.
- ❌ External calls (payments, e-mail) are covered only as far as the outbox entry. Your downstream must be idempotent.
- ❌ The effect must be in the same database as TaskFence's tables. Federated multi-service mode is planned for v0.2.

Read [docs/LIMITATIONS.md](https://github.com/mohdmuzeeb/taskfence/blob/main/docs/LIMITATIONS.md) before relying on it.

## Testing your own code for revocation races

`taskfence.testing` stages races deterministically with latches, and checks the database with an independent auditor:

```python
from taskfence.testing import assert_no_violations, hook, run_in_thread

# `taskfence_latches` is a fixture that ships with taskfence; `fence`, `conn` and `grant` are yours.
def test_revoke_while_refund_runs(taskfence_latches, fence, conn, grant):
    token = fence.issue(conn, grant.grant_id, task_id="t1")
    pause = taskfence_latches.pause_at("app.before_txn")    # your code calls hook("app.before_txn")
    worker = run_in_thread(lambda: my_refund(token))
    pause.wait_reached()
    fence.revoke(conn, grant.grant_id)                      # revoke while the task is mid-flight
    pause.release()
    worker.join()
    assert_no_violations(conn)                              # no commit after the revocation
```

## Using with MCP servers

[examples/03_mcp_server](https://github.com/mohdmuzeeb/taskfence/tree/main/examples/03_mcp_server) is a refund-desk MCP server whose background refunds are fenced by TaskFence. A demo drives it with the official MCP client and shows four cases:
- a refund revoked mid-task is blocked
- a refund cancelled mid-task is blocked
- a refund on another customer's order is blocked
- a refund after a fresh grant commits

The Python MCP SDK does not include the MCP Tasks extension yet, so the example uses start-then-poll tools.

## Roadmap

- **v0.2:** a federated grant authority (signed service-to-service RPC), TypeScript middleware for the MCP Tasks extension, and an async API
- **v0.3:** `taskfence scan`, a revocation-race checker for MCP servers

## Research and citation

TaskFence is based on peer-reviewed research:

> M. Mohammad, "TaskFence: Commit-Time Authorization and Revocation Safety for Long-Running MCP Agent Workflows," *International Conference on Intelligent Cybernetics Technology & Applications (ICICyTA 2026)*.

If you use TaskFence, please cite the paper. To cite the software itself, use its Zenodo DOI, [10.5281/zenodo.23842363](https://doi.org/10.5281/zenodo.23842363), which always points to the latest version. GitHub's "Cite this repository" button uses [CITATION.cff](https://github.com/mohdmuzeeb/taskfence/blob/main/CITATION.cff). The archived research artifact (code, preregistration, wire traces, raw databases) will be linked here once it has a DOI.

## Contributing and security

Issues and pull requests are welcome; see [CONTRIBUTING.md](https://github.com/mohdmuzeeb/taskfence/blob/main/CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](https://github.com/mohdmuzeeb/taskfence/blob/main/SECURITY.md).

## License

MIT
