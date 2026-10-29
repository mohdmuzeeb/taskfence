# TaskFence

**Revocation-safe side effects for long-running AI agent tasks.**
If a user revokes permission while an agent task is running, TaskFence guarantees that the task cannot commit afterwards. The check happens inside your own database transaction, not before it.

<!-- badges: PyPI, CI, license, DOI -->

> Status: v0.1 (pre-release). Draft README outline; sections marked TODO are filled in at release.

## The problem

<!-- TODO: 6-line timeline diagram -->
1. An agent task is admitted: the user has granted `refund.issue`.
2. The task works for 10 minutes (LLM calls, tool calls, waiting for input).
3. The user revokes the grant at minute 4.
4. At minute 10 the task writes the refund. **Most servers allow this**, because they checked permission at the start or just before the write, in a separate step.

The TaskFence paper measured this window. Checking at start, or before execution in a separate step, let hundreds of commits through after revocation in randomized tests. Checking at commit time let none through.

## Install

```bash
pip install taskfence              # SQLite, standard library only
```

```bash
pip install "taskfence[postgres]"  # PostgreSQL via psycopg 3
```

```python
from taskfence.stores.postgres import PostgresStore, connect
fence = Fence(KeyRing.from_env(), PostgresStore())     # fair shared locking per grant (default)
```

## Running the tests

```bash
pytest                                             # SQLite only
```

```bash
docker run -d --rm --name taskfence-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:16 -c track_commit_timestamp=on
```

```bash
TASKFENCE_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/postgres pytest     # SQLite + PostgreSQL
```

## 30-second example

```python
with fence.transaction(conn):          # do slow work (LLM calls) BEFORE this line
    fence.guard(conn, token, operation="refund.issue",
                resource="order:9001", operation_id=f"{task_id}:refund")
    conn.execute("UPDATE orders SET refunded = 1 WHERE id = 'order:9001'")
```

The full runnable version, with permission revoked mid-task, is [examples/01_quickstart_sqlite.py](examples/01_quickstart_sqlite.py). It prints `refund blocked: STALE_EPOCH`.

## How it works
- Grants have **epochs**. Revoking a grant raises a **fence** (`min_epoch`) in your database.
- Tasks carry a signed **token** that names the epoch they were admitted under.
- `guard()` checks the token against the fence **in the same transaction as the effect**. The commit either sees the revocation or happens strictly before it.
- It also fences cancelled tasks and makes operations idempotent.

Details: [docs/DESIGN.md](docs/DESIGN.md).

## What it guarantees, and what it does not
<!-- TODO: short version of docs/LIMITATIONS.md; keep it prominent -->
- ✅ No effect protected by `guard()` commits after revocation becomes effective.
- ✅ Revocation waits for at most one in-flight guarded transaction.
- ❌ Not retroactive: effects that committed before revocation stay committed.
- ❌ External calls (payments, e-mail) are covered only as far as an outbox entry. Your downstream must be idempotent.
- ❌ v0.1 assumes the grant state lives in the same database as the protected data. Federated mode is planned for v0.2.

## Testing your own code
<!-- TODO: taskfence.testing example with RevocationRace + audit() -->

## Using with MCP servers
[examples/03_mcp_server](examples/03_mcp_server) is a refund-desk MCP server with background refunds that are fenced by TaskFence. A demo drives it with the official MCP client and shows four cases:
- a refund revoked mid-task is blocked
- a refund cancelled mid-task is blocked
- a refund on another customer's order is blocked
- a refund after a fresh grant commits

The Python MCP SDK does not include the MCP Tasks extension yet, so the example uses start-then-poll tools. Native Tasks middleware is planned for v0.2, in TypeScript.

## Roadmap
- v0.2: federated grant authority (signed service-to-service RPC); TypeScript MCP Tasks middleware
- v0.3: `taskfence scan`, a revocation-race checker for MCP servers

## Research and citation
TaskFence is based on peer-reviewed research:

> M. Mohammad, "TaskFence: Commit-Time Authorization and Revocation Safety for Long-Running MCP Agent Workflows," *ICICyTA 2026*. <!-- TODO: DOI -->

The reproducible research artifact (code, preregistration, wire traces, raw databases) is at <!-- TODO: Zenodo DOI -->. See [CITATION.cff](CITATION.cff).

## Contributing and security
Issues and PRs are welcome; see CONTRIBUTING.md. Report vulnerabilities privately as described in SECURITY.md.

## License
MIT
