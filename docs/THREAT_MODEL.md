# Threat model

This covers the TaskFence **library** (v0.1, embedded mode, plus the owner-side federated API). The research harness behind the paper has its own threat model in the archived artifact.

## Safety property

> Once revocation of a grant is **effective**, no effect admitted under a revoked epoch commits. Once `cancel_task` has committed, no operation of that task commits.

**When revocation becomes effective:**
- **Embedded mode:** when `fence.revoke()` returns `effective=True`, or when the caller's transaction that ran it commits.
- **Federated mode:** once every affected owner has returned a FENCE receipt. Collecting those receipts is the grant authority's job, which is not in v0.1.

**Revocation is about order, not wall-clock time.** An effect whose guarded transaction commits before the revocation stands. A revocation issued while a guarded transaction is open waits for that transaction, at most one transaction per in-flight guard.

## Trusted

- **The database's transactional guarantees.**
  - SQLite: one writer at a time, atomic commit, WAL mode.
  - PostgreSQL: row locks, transaction-level advisory locks, and atomic commit at READ COMMITTED, REPEATABLE READ or SERIALIZABLE.
- **The control plane:** code that calls `grant`, `revoke`, `regrant`, `cancel_task`, `cancel_operation`, `apply_install` and `apply_fence`. It decides who may revoke; TaskFence does not authenticate it.
- **The application's protected code path.** The effect must be written in the same transaction as `guard()`, on the same connection, before COMMIT. TaskFence cannot see effects that bypass it.
- **The signing keys** in the `KeyRing`, and the process that holds them.
- **The `taskfence_*` tables.** Only TaskFence and the trusted control plane write them.

## Untrusted

- **The agent, its workers, and anything they carry.** This includes the token, `operation_id`, `resource`, timing and retries. Tokens can be stale, replayed, duplicated, delayed, or tampered with.
- **Adversarial timing.** A worker can pause anywhere for any length of time, for example from LLM latency, GC, network delay, or a deliberately slow tool.
- **Concurrent duplicate workers** for the same operation.
- **Tokens that are correctly signed but name another principal, operation, resource or epoch.** The fence row and scope reject them inside the transaction.

## Out of scope: what the adversary is assumed unable to do

- Read or use the signing keys. A leaked key lets an attacker mint tokens for any grant; see *Key compromise*.
- Write to the database other than through the application's protected path.
- Undo or intercept an external, irreversible side effect. TaskFence only guarantees that an outbox entry exists if and only if the guarded transaction committed.
- Change the application's own authorization decision about *who* may grant or revoke.

## Mechanisms and evidence

| # | Property | Mechanism | Tests |
|---|---|---|---|
| P1 | Post-revocation safety | The fence check and the effect run in one transaction: SQLite's conditional UPDATE under the write lock, or PostgreSQL's advisory plus row lock. | `test_races.py` S2–S5, randomized concurrency; `test_pg_isolation.py` |
| P2 | Epoch freshness | Token epoch ≥ `min_epoch` and ≤ issued `epoch`; the floor only increases. | `test_guard.py` regrant and future-epoch tests; S5 |
| P3 | Atomicity | The lock is held from the check until COMMIT. A revoke waits. | S4b (`revoke` blocks while a guard is open) |
| P4 | At most one effect per `operation_id` | Unique key on `taskfence_ops`; explicit `Duplicate` | S6, `test_duplicate_and_cancelled_operations` |
| P5 | Cancellation fence | SQLite: global write lock. PostgreSQL: the per-task row is locked by the guard and written by `cancel_task`. | S7, `test_task_cancellation_is_fenced`, `test_cancel_after_snapshot_never_commits` |
| P6 | Fail closed | Any exception before COMMIT rolls back. Lock timeouts and serialization failures raise `Retry`. | S8b, S8c; deferred-transaction and isolation tests |
| P7 | Scope and principal | Principal, operation and resource are matched against the fence row and scope; an optional application predicate runs in the same transaction. | `test_guard.py` scope, mismatch and predicate tests; MCP example |
| P8 | Independent judgment | The auditor is SQL over the final database, ordered by `seq` on SQLite and by commit timestamp on PostgreSQL. | Unsafe B1/B2 baselines are flagged; all safe runs are clean |

## Key compromise

1. Generate a new key and make it the signing key. Keep the old `kid` only if tokens signed with it must keep working.
2. Remove the compromised `kid` from every `KeyRing`. Its tokens then fail with `UNKNOWN_KEY`.
3. `revoke` every grant that could have been abused. This raises `min_epoch`, so tokens are dead even if the key is still accepted somewhere.

**HMAC is symmetric.** Anyone who can verify a token can also mint one. Federated deployments where owners should verify but not mint need asymmetric signatures, planned for v0.2.

## Known gaps (tracked for v0.2+)

- No networked grant authority and no signed service-to-service RPC. Federated mode is owner-side only.
- No issuer identity or audience claim in tokens, so a token is valid wherever its `KeyRing` is.
- No asymmetric signatures.
- Not reviewed by a third party.
