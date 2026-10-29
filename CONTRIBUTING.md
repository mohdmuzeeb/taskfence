# Contributing

Thanks for helping. Issues, bug reports and pull requests are welcome.

## Development setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test,postgres,examples]"
```

```bash
.venv/bin/python -m pytest                     # SQLite + MCP example
```

PostgreSQL tests need a server with commit timestamps enabled:

```bash
docker run -d --rm --name taskfence-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:16 -c track_commit_timestamp=on
```

```bash
TASKFENCE_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/python -m pytest
```

## Ground rules for changes

- **Safety changes come with a race test.** Anything touching `guard()`, a store, or the control plane needs a latch-scheduled test in `tests/test_races.py`, or `tests/test_pg_isolation.py` for PostgreSQL-specific behavior. The test should fail without the change, and `assert_no_violations()` should pass with it.
- **Every back-end runs the same tests.** New behavior is tested through `conftest.py`'s `backend` fixture, so it runs on SQLite, `pg-shared` and `pg-exclusive`.
- **The core has no runtime dependencies.** The core uses only the standard library; drivers are optional extras.
- **Claims match evidence.** If a change affects what TaskFence guarantees, update `docs/THREAT_MODEL.md` and `docs/LIMITATIONS.md` in the same pull request.

Report security issues privately; see [SECURITY.md](SECURITY.md).
