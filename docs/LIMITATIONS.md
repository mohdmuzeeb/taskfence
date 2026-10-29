# Limitations (plain language)

Read this before relying on TaskFence or citing its results.

1. **Your effect must live in the same database as TaskFence's tables.** The guarantee comes from one transaction covering both the fence check and your write. If the effect is in another database or service, use the outbox (item 3), or wait for federated mode (v0.2).
2. **Only effects written inside the guarded transaction are protected.** A write made before `guard()` in another transaction, on another connection, or after COMMIT is not covered.
3. **External calls are covered only as far as the outbox.** `enqueue()` guarantees the entry exists if and only if the guarded transaction committed. Delivering it, and making the downstream idempotent, is your job. A payment API called directly from inside the transaction is not protected by TaskFence.
4. **Revocation is not retroactive, and it can wait for one transaction.**
   - An effect that committed before the revocation stands.
   - A revoke issued while a guarded transaction is open waits for it.
   - Keep guarded transactions short, and do slow work (LLM calls, tool calls) *before* opening them.
5. **SQLite: `fence.transaction()` takes the write lock at BEGIN.** One guarded transaction runs at a time per database file. This is SQLite's model, and it is fine for small services. Use PostgreSQL for concurrency.
6. **PostgreSQL is newer than the paper.**
   - The paper evaluated SQLite only.
   - The PostgreSQL store is tested on Postgres 16 on one host: race scenarios, three isolation levels, and revocation latency under load. That covers no other versions, no networked servers, and no failover or replicas.
   - Its auditor needs `track_commit_timestamp = on`.
7. **`Retry` is a real outcome.** Lock timeouts, and serialization failures under REPEATABLE READ or SERIALIZABLE, surface as `Retry`. Nothing committed; the caller decides whether to try again with the same `operation_id`.
8. **Performance numbers are indicative only.** The latency figures in `DESIGN.md` come from one machine, small N, and loopback connections. They are not an overhead claim.
9. **Synchronous API only.** There is no `asyncio` API yet; async servers call TaskFence through `asyncio.to_thread`, as the MCP example does.
10. **No MCP Tasks extension integration.** The Python MCP SDK does not ship the Tasks extension. The MCP example uses start-then-poll tools.
11. **TaskFence does not authenticate anyone.** It enforces grants; deciding who may create or revoke them, and who the principal is, is up to your application.
12. **Not independently audited.** v0.1 is pre-release. See `THREAT_MODEL.md` for known gaps.
