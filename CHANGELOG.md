# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, minor versions may change the API.

## [0.1.0] - unreleased

The first release: a library implementation of the commit-time authorization design from *TaskFence: Commit-Time Authorization and Revocation Safety for Long-Running MCP Agent Workflows* (ICICyTA 2026).

### Added
- **The `Fence` API.**
  - `grant`, `revoke`, `regrant` and `issue` (signed task tokens)
  - `guard()`, which authorizes an effect inside the transaction that commits it, plus the `@fenced` decorator
  - `cancel_task` and `cancel_operation` fences
  - idempotent `operation_id`s, with an explicit `Duplicate` outcome
  - an optional application predicate, checked inside the same transaction
  - a transactional outbox: `enqueue` and `drain_outbox`
  - owner-side federated messages: `apply_install` and `apply_fence`, idempotent and with a monotone floor
- **Tokens.** `tf1` format, HMAC-SHA256, and a `KeyRing` with key rotation. Standard library only; no runtime dependencies.
- **SQLite store.** WAL mode; the fence check is a conditional UPDATE under the write lock, as evaluated in the paper.
- **PostgreSQL store** (`taskfence[postgres]`), with two lock modes:
  - `lock="shared"` (default): a fair advisory lock plus `FOR SHARE`
  - `lock="exclusive"`: `FOR UPDATE`

  It is safe at READ COMMITTED, REPEATABLE READ and SERIALIZABLE, and is tested in CI on Postgres 15, 16 and 17.
- **`taskfence.testing`.** Latches for deterministic races, an independent SQL auditor (`audit`, `assert_no_violations`), and a `taskfence_latches` pytest fixture.
- **Examples.** A SQLite quickstart, and an MCP refund-desk server with a demo client (`taskfence[examples]`).
- **Docs.** Design, threat model and limitations.

### Known limitations
See [docs/LIMITATIONS.md](docs/LIMITATIONS.md). In short:
- the effect must be in the same database as TaskFence's tables
- external calls are covered only as far as the outbox
- there is no async API yet
- there is no MCP Tasks extension integration yet
