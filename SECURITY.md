# Security policy

TaskFence is a security library, so reports are very welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through GitHub's private vulnerability reporting: the **Security** tab of this repository, then **Report a vulnerability**.

Include:
- the TaskFence version
- the store (SQLite or PostgreSQL and its version)
- the isolation level
- a minimal reproduction, ideally a test using `taskfence.testing` latches and `audit()`

You should get an acknowledgment within 7 days. Fixes for confirmed issues are released with a GitHub security advisory that credits the reporter, unless they prefer otherwise.

## What counts

Anything that lets an effect commit after its revocation or cancellation is effective, as defined in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Also reportable: token forgery, a scope or principal bypass, or a way to make the auditor miss a violation.

The limitations listed in [docs/LIMITATIONS.md](docs/LIMITATIONS.md) are known and documented, not vulnerabilities.

## Supported versions

Only the latest release receives fixes during the 0.x series.
