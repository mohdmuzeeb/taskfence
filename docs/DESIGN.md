# TaskFence v0.1: design

Status: draft, 2026-10-28
Author: Muzeeb Mohammad
Source research: *TaskFence: Commit-Time Authorization and Revocation Safety for Long-Running MCP Agent Workflows* (ICICyTA 2026, paper #1571370151) and its artifact (Phases 1–4).

## 1. What v0.1 is

The paper's artifact is a **research harness**: synthetic `replace_card` operations, latched workers, and an auditor that proves the result. v0.1 turns the mechanism inside it into a **library that application developers can install** (`pip install taskfence`) and put in front of any side effect that an AI agent task performs.

**The problem.** An agent task is admitted while the user's permission is valid. The task then runs for seconds, minutes or hours. The permission is revoked in the meantime, and the task still commits its write, refund, or delete. Checking permission at task start (B0/B1 in the paper) or just before the write (B2) leaves that window open. Phase 2 recorded 317 provable post-revocation commits for these designs in one randomized run.

**The fix (from D5 in the paper).** Check the grant's epoch **inside the same database transaction that commits the effect**, against a monotone fence row that revocation updates. A revocation that has committed (or, in federated mode, been acknowledged by every owner) can no longer be outrun by any task.

**One-sentence pitch for the README:** *Revocation-safe side effects for long-running AI agent tasks: if permission is revoked, no task commits after that, guaranteed by your own database transaction.*

## 2. Goals and non-goals

### Goals
1. G1: A small API, `guard()`, that a developer calls inside their own transaction just before the effect.
2. G2: Back-ends for **SQLite** (as in the paper) and **PostgreSQL** (where real adoption will happen).
3. G3: **Embedded mode**: grant state and protected data in one database. Revocation becomes effective when its transaction commits.
4. G4: An **owner-side fence API** (`apply_fence`, `apply_install`), so that federated (D5) deployments can be built on top of it now and supported fully in v0.2.
5. G5: **Task cancellation fencing**. This closes limitation #4 of Phase 1: a cancelled task can no longer commit.
6. G6: **Idempotent operations** (`operation_id`), with an explicit `DUPLICATE` outcome.
7. G7: A **testing module** (latches plus an auditor), so users can show in their own test suite that no commit happens after revocation.
8. G8: An honest threat model and limitations section, carried over from the paper.

### Non-goals for v0.1 (in the roadmap)
- A networked grant-authority service with signed service-to-service RPC (v0.2, built from Phase 3A `ga.py` and `s2s.py`).
- TypeScript / MCP Tasks extension middleware (v0.2, built from the Phase 3A `adapter/`).
- A black-box race scanner for third-party MCP servers (v0.3).
- Guarantees for external, irreversible calls such as payment processors or e-mail. v0.1 gives an outbox entry, nothing more (§7).
- Retroactive revocation, a formal proof, and multi-region databases.

## 3. Core concepts

| Concept | Meaning |
|---|---|
| **Grant** | Permission for `principal` to perform `operation` on a set of `resources`. Identified by `grant_id`. |
| **Epoch** | Integer version of a grant. Each regrant increments it. Tokens carry the epoch they were issued under. |
| **Fence row** | Row in the protected database holding `min_epoch` for a grant. A token whose epoch is below `min_epoch` is dead. It only moves up. |
| **Token** | Signed claim `{grant_id, principal, operation, epoch, task_id, kid, iat, exp}` issued at task admission and carried by the worker. |
| **Guard** | The check run **inside** the effect's transaction: signature, expiry, fence, scope, cancellation and idempotency. |
| **Revocation effective** | The point from which the guarantee holds. In embedded mode this is when the revoke transaction commits. In federated mode it is when every affected owner has returned a fence receipt. |

**Safety property (same as the paper):** once revocation of epoch *e* is effective, no effect admitted under an epoch ≤ *e* commits.

## 4. Public API (Python)

```python
from taskfence import Fence, KeyRing, Denied
from taskfence.stores.postgres import PostgresStore      # or taskfence.stores.sqlite.SQLiteStore

fence = Fence(store=PostgresStore(), keys=KeyRing.from_env("TASKFENCE_KEYS"))
fence.migrate(conn)                                      # creates taskfence_* tables

# --- admin / control plane (embedded mode) -------------------------------
g = fence.grant(conn, principal="user:42", operation="refund.issue",
                resources=["order:9001"])                # -> Grant(grant_id, epoch=1)
fence.revoke(conn, g.grant_id)                           # -> Revocation(effective=True, min_epoch=2)
fence.regrant(conn, g.grant_id)                          # -> Grant(epoch=2)
fence.cancel_task(conn, task_id="t-123")                 # cancellation fence

# --- task admission ------------------------------------------------------
token = fence.issue(conn, g.grant_id, task_id="t-123", ttl_s=3600)   # str (compact, signed)

# --- worker, possibly much later -----------------------------------------
with conn.transaction():
    fence.guard(conn, token,
                operation="refund.issue",
                resource="order:9001",
                operation_id="t-123:refund:order:9001")  # raises Denied(code) / Duplicate
    conn.execute("UPDATE orders SET refunded = true WHERE id = %s", (9001,))
# commit -> effect and fence check are atomic
```

A convenience decorator for the common case where the library owns the transaction:

```python
@fence.fenced(operation="refund.issue",
              resource=lambda order_id, **_: f"order:{order_id}",
              operation_id=lambda task_id, order_id, **_: f"{task_id}:refund:{order_id}")
def issue_refund(conn, *, token, task_id, order_id): ...
```

**Outcomes.** `guard()` returns `Committed` (provisional; it becomes real when the caller commits), or raises `Duplicate` or `Denied(code)`. The denial codes are carried over from the Phase 3A `owner._diagnose`:
`BAD_SIGNATURE, UNKNOWN_KEY, EXPIRED, NO_GRANT, STALE_EPOCH, FUTURE_EPOCH, PRINCIPAL_MISMATCH, OPERATION_MISMATCH, RESOURCE_NOT_IN_SCOPE, TASK_CANCELLED, OPERATION_CANCELLED`.

The guard **never commits or rolls back** the caller's transaction. It only reads, locks and inserts rows. When it raises, the caller's transaction is still open and the caller is expected to roll it back. Context-manager helpers do this automatically.

**Application-specific checks.** In the paper, "the card belongs to the principal" was hard-coded. In the library this becomes an optional `predicate(conn, token, resource) -> bool`, executed inside the same transaction after the fence check.

**Federated (owner-side) API, low-level, for v0.2 groundwork:**
```python
fence.apply_install(conn, msg)   # INSTALL{grant_id, principal, operation, epoch, resources}
fence.apply_fence(conn, msg)     # FENCE{grant_id, fence_epoch, revocation_id}
# both idempotent by msg_id, both return a receipt; floor = max(floor, epoch)
```

## 5. Storage schema

The same tables exist on both back-ends (Postgres types shown). All names are prefixed `taskfence_` and live in the **application's own database**. That shared transactional domain is the whole point.

```sql
CREATE TABLE taskfence_fences (
  grant_id     text PRIMARY KEY,
  principal    text   NOT NULL,
  operation    text   NOT NULL,
  epoch        bigint NOT NULL,      -- highest issued epoch (embedded mode)
  min_epoch    bigint NOT NULL,      -- tokens with epoch < min_epoch are dead
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE taskfence_scope (
  grant_id text NOT NULL REFERENCES taskfence_fences ON DELETE CASCADE,
  resource text NOT NULL,
  PRIMARY KEY (grant_id, resource)
);
CREATE TABLE taskfence_ops (          -- idempotency + audit
  operation_id text PRIMARY KEY,
  state        text NOT NULL CHECK (state IN ('COMMITTED','CANCELLED')),
  task_id      text NOT NULL,
  grant_id     text NOT NULL,
  epoch        bigint NOT NULL,
  resource     text,
  committed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE taskfence_cancelled_tasks (task_id text PRIMARY KEY, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE taskfence_receipts (msg_id text PRIMARY KEY, kind text NOT NULL, grant_id text NOT NULL,
  epoch bigint NOT NULL, applied boolean NOT NULL, min_epoch bigint NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE taskfence_outbox (id bigserial PRIMARY KEY, operation_id text NOT NULL REFERENCES taskfence_ops,
  topic text NOT NULL, payload jsonb NOT NULL, delivered_at timestamptz);
```

Embedded-mode state transitions, each one a single transaction on the fence row:

| Action | `epoch` | `min_epoch` |
|---|---|---|
| grant | 1 | 1 |
| revoke | unchanged *e* | *e* + 1 (all issued tokens dead) |
| regrant | *e* + 1 | *e* + 1 |

A grant is *active* iff `min_epoch <= epoch`. There is no separate `active` flag, so there is only one source of truth.

## 6. Guard algorithm and concurrency

Steps 1–2 run outside the database. Steps 3–7 run inside the caller's open transaction.

1. Decode the token and verify its HMAC with the key named by `kid` (constant-time compare).
2. Check `exp` against the clock (configurable skew).
3. **Lock and check the fence row** (back-end specific, see below):
   `epoch_tok >= min_epoch AND epoch_tok <= epoch AND principal/operation match`.
   The second condition (`FUTURE_EPOCH`) is a cheap defense against high-epoch token abuse in embedded mode.
4. Check that the resource is in `taskfence_scope`.
5. Check that `task_id` is not in `taskfence_cancelled_tasks`.
6. Run the optional application `predicate`.
7. `INSERT INTO taskfence_ops (... 'COMMITTED' ...)`. A unique violation returns `Duplicate`, or `Denied(OPERATION_CANCELLED)` if the existing row is `CANCELLED`.

If a check fails, the library then runs a diagnosis query to return the precise code, as `_diagnose` does in Phase 3A.

### Back-end locking

**SQLite.** Same as the paper. The library requires `BEGIN IMMEDIATE`, or a conditional `UPDATE` on the fence row as the first write, so the transaction holds the single writer lock. Revoke is also a write, so the two serialize. Only WAL mode is supported in v0.1, the mode that was tested.

**PostgreSQL (implemented in `stores/postgres.py`; new, not covered by the paper).** There are two modes, `PostgresStore(lock=...)`:
- **`"shared"` (default).**
  - The guard takes `pg_advisory_xact_lock_shared(grant)`, then reads the fence row `FOR SHARE`.
  - `revoke`, `regrant` and federated messages take the exclusive advisory lock, then `UPDATE` the row.
  - Guards for one grant run in parallel. Advisory locks queue fairly, so a waiting revoke is not overtaken by new guards.
  - The `FOR SHARE` is still needed under REPEATABLE READ and SERIALIZABLE. The advisory-lock statement takes the snapshot *before* waiting, so only the row lock detects a fence that changed after the snapshot.
- **`"exclusive"`.** The guard reads the fence row `FOR UPDATE`, so guards for one grant serialize (as in D5).
- **Cancellation.** The guard ensures a `taskfence_tasks` row exists and reads it `FOR SHARE`. `cancel_task` writes that row, so the two serialize. Cancellations are also appended to `taskfence_cancel_log` for the auditor.
- **Federated floor advance.** A conditional `UPDATE` runs before the row lock.
- **Isolation.** Under READ COMMITTED a stale guard is `Denied` (each statement re-reads after its lock). Under REPEATABLE READ and SERIALIZABLE it gets a serialization failure, surfaced as `Retry`.
- **Commit order.** `seq` comes from a sequence. It is consistent with commit order for *conflicting* operations, because it is taken after the locks, but not globally. The auditor therefore compares `pg_xact_commit_timestamp(xmin)` and needs `track_commit_timestamp = on`.

**Evidence (2026-10-28).** Postgres 16.15 in Docker on one macOS host, psycopg 3.3.6, Python 3.13.
- **The full shared suite passes on SQLite, pg-shared and pg-exclusive** (161 passed). The suite covers:
  - S1–S8 latch scenarios
  - B1/B2 unsafe baselines, which the auditor flags
  - randomized concurrency with zero post-revocation commits
  - federated messages and the outbox
- **`tests/test_pg_isolation.py`, at all three isolation levels:**
  - A revocation or cancellation committed after the worker's snapshot never commits.
  - Under READ COMMITTED the result is `Denied`; under REPEATABLE READ and SERIALIZABLE it is `Retry`.
- **Revoke latency** with 8 threads keeping overlapping 20 ms guarded transactions open on one grant, over 5 runs:

  | Mode | Revoke latency |
  |---|---|
  | shared + advisory lock | 11–53 ms |
  | exclusive | 253–326 ms |
  | plain `FOR SHARE` (no advisory lock, test-only) | 9–1732 ms |

  These are indicative single-host numbers, not a performance claim. The result is that the advisory lock keeps revocation delay near one transaction, while plain `FOR SHARE` is erratic.

**Decision: `lock="shared"` is the default.** Use `"exclusive"` only when a grant sees few concurrent guards and a simpler lock story is preferred.

**Trade-off to document:** every guarded commit touches one row per grant. Option A spreads the contention, but a revoke has to wait for long-running guarded transactions. The guidance is to keep the guarded transaction short and to do slow work *before* `guard()`.

## 7. External side effects (outbox)

`fence.outbox.enqueue(conn, operation_id, topic, payload)` inserts into `taskfence_outbox` in the same transaction as `guard()`. A minimal polling relay, `taskfence.outbox.Relay`, hands each entry to a user callback at least once and marks it delivered.

The documented guarantee is deliberately narrow: **the entry exists iff the fenced transaction committed.** Whether the downstream processor completes, and whether it is idempotent, is the user's responsibility. This follows assessment point 4 and must not be generalized.

## 8. Tokens and keys

- Format: `tf1.<base64url(json claims)>.<base64url(hmac-sha256)>`. Standard library only; no JWT dependency.
- `KeyRing` holds several keys, each identified by its `kid`.
  - Exactly one key is the *signing* key; the others are verify-only. This gives rotation without downtime.
  - Keys load from an environment variable or a file and are never logged.
- Compromised key: remove its `kid` (its tokens become `UNKNOWN_KEY`) and revoke the affected grants (`min_epoch` moves up). Both steps are documented in `THREAT_MODEL.md`.
- Optional Ed25519 (`taskfence[ed25519]`, via `cryptography`) is planned for federated mode in v0.2, where owners should verify but not mint tokens.
- Assessment items still open for v0.2: issuer identity and claim binding across services, and loss-of-signing-key analysis for federated owners.

## 9. Testing (in-repo and shipped to users)

**Ported from the artifact:**
- Phase 1 `test_invariants.py`, `test_races.py`, `test_idempotency.py`: scenarios S1–S8d rewritten against the library API, run on SQLite **and** Postgres (Postgres via Docker / `testcontainers`, marked so the suite can run SQLite-only offline).
- Baselines B0–B2 kept as test-only "unsafe" implementations, so that the suite shows the window is reachable without TaskFence and closed with it. This is also the demo in the README.
- Mutation checks (the idea behind Phase 3A `scripts/mutation.py`). Moving `guard()` outside the transaction, dropping `FOR SHARE`, or skipping the cancellation check must each make the suite fail.

**Shipped as `taskfence.testing` (for users' own suites):**
- `latch("name")` hook points inside `guard()`: `before_fence_check`, `after_fence_check`, `before_ops_insert`. They are no-ops unless a test installs a controller.
- `RevocationRace` helper: pause a worker at a hook, run `revoke`, release the worker, and expect `Denied(STALE_EPOCH)`.
- `audit(conn)`: an independent SQL check that no `taskfence_ops` row has `epoch < min_epoch` at a commit position after the revocation receipt. It is a port of the adjudicator idea from Phase 1 and `audit.py` from Phase 3B.
- A pytest plugin providing the `taskfence_race` fixture.

## 10. Repository layout

```
taskfence/
├── pyproject.toml            # name, extras: postgres (psycopg>=3.2), ed25519, test
├── README.md
├── LICENSE                   # MIT (same as the artifact)
├── CITATION.cff              # paper + Zenodo DOI of the artifact
├── CHANGELOG.md
├── SECURITY.md               # private disclosure address
├── src/taskfence/
│   ├── __init__.py           # Fence, KeyRing, Denied, Duplicate, Retry
│   ├── fence.py              # facade: grant/revoke/regrant/issue/guard/cancel_task/fenced
│   ├── tokens.py             # tf1 format, KeyRing, HMAC
│   ├── outcomes.py           # codes, exceptions
│   ├── federated.py          # apply_install / apply_fence / receipts
│   ├── outbox.py             # enqueue + Relay
│   ├── stores/
│   │   ├── base.py           # Store protocol (lock_fence, insert_op, ...)
│   │   ├── sqlite.py
│   │   ├── postgres.py
│   │   └── sql/{sqlite,postgres}.sql
│   └── testing/{latches.py, race.py, audit.py, plugin.py}
├── examples/
│   ├── 01_quickstart_sqlite.py         # 40 lines: grant, issue, revoke mid-task, Denied
│   ├── 02_refund_agent_postgres/       # FastAPI + background task + revoke endpoint
│   └── 03_mcp_server/                  # MCP tool that runs a long task and is fenced
├── tests/
│   ├── scenarios/            # S1–S8d ported
│   ├── test_tokens.py
│   ├── test_sqlite_races.py
│   ├── test_postgres_races.py
│   ├── test_mutations.py
│   └── unsafe_baselines.py   # B0–B2 (test only)
└── docs/
    ├── DESIGN.md             # this file
    ├── THREAT_MODEL.md       # from artifact docs/threat_model.md, updated for library use
    ├── LIMITATIONS.md        # from artifact, plus Postgres-specific notes
    └── RESEARCH.md           # how v0.1 relates to D5/B3, link to paper + artifact DOI
```

The research artifact stays a separate, frozen repository (archived on Zenodo) so that the paper's results stay reproducible. The library's README links to it, and the artifact's README links to the library.

## 11. v0.1 release checklist

- [ ] Zenodo DOI for the artifact is reserved and in the camera-ready paper and `CITATION.cff`
- [x] S1–S8d and randomized concurrency tests are green on SQLite and on Postgres 15–17 (CI, 2026-10-29). Automated mutation tests are still to do.
- [ ] Postgres locking option chosen and the evidence recorded in `docs/RESEARCH.md`
- [ ] `examples/01` runs with zero configuration; `examples/03` runs against a real MCP client
- [ ] README, THREAT_MODEL, LIMITATIONS and SECURITY reviewed
- [ ] CI on GitHub Actions (3.12, 3.13; SQLite + Postgres service)
- [ ] Published to PyPI with trusted publishing; GitHub release notes
- [ ] Adoption tracking started: pepy.tech, GitHub dependents, a log of outside issues and PRs

## 12. Open questions

1. **Employer IP and outside-work policy.** Confirm that the employment agreement allows publishing this as a personal open-source project. Do this before the first public commit.
2. **Package name.** Check that `taskfence` is free on PyPI and npm, and reserve it early.
3. ~~**MCP Python SDK and the Tasks extension.**~~ Resolved 2026-10-28: `mcp` 2.3.0 (protocol 2026-07-28) has a generic `Extension` interface but no `io.modelcontextprotocol/tasks` implementation, and no Python package for it exists on PyPI. `examples/03_mcp_server` therefore uses start-then-poll tools. Native Tasks middleware ships in v0.2 in TypeScript.
4. ~~**Postgres lock choice.**~~ Resolved: `lock="shared"` with a fair advisory lock (§6, Evidence). Postgres 15 and 17 pass the full suite in CI (2026-10-29). Still open: a networked (non-loopback) server.
5. **Async API.** `asyncpg` / `psycopg` async support in v0.1 or v0.1.x? Many MCP servers are async, which argues for at least `psycopg` async in v0.1.

## 13. Effort estimate

Roughly 3–4 weeks of part-time work: one week for core, tokens and SQLite with the ported tests; one week for Postgres with race evidence; one week for examples, docs and CI; then a buffer and release.
