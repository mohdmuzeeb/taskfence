"""The user's control plane for the refund desk: a separate process from the MCP server.

    python admin.py setup      create tables and the refund grant
    python admin.py revoke     revoke the agent's refund access (in-flight refunds can no longer commit)
    python admin.py regrant    grant access again (new epoch; old tasks stay blocked)
    python admin.py show       orders, refund tasks, outbox and an audit of the safety property
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
from taskfence.stores.sqlite import connect  # noqa: E402
from taskfence.testing import audit  # noqa: E402


def revoke() -> str:
    c = connect(app.DB)
    try:
        r = app.open_fence().revoke(c, app.grant_id())
        return f"revoked: effective={r.effective} min_epoch={r.min_epoch}"
    finally:
        c.close()


def regrant() -> str:
    c = connect(app.DB)
    try:
        g = app.open_fence().regrant(c, app.grant_id())
        return f"regranted: epoch={g.epoch}"
    finally:
        c.close()


def show() -> str:
    c = connect(app.DB)
    try:
        lines = ["orders:"]
        for r in c.execute("SELECT id, customer, total_cents, refunded_cents FROM orders ORDER BY id"):
            lines.append(f"  {r[0]}  {r[1]}  total={r[2]}  refunded={r[3]}")
        lines.append("refund tasks:")
        for r in c.execute("SELECT task_id, order_id, amount_cents, status, code FROM refund_tasks ORDER BY created_at"):
            lines.append(f"  {r[0]}  {r[1]}  {r[2]}  {r[3]}{'  ' + r[4] if r[4] else ''}")
        lines.append("outbox (payments to send):")
        for r in c.execute("SELECT operation_id, topic, payload FROM taskfence_outbox ORDER BY id"):
            lines.append(f"  {r[0]}  {r[1]}  {r[2]}")
        v = audit(c)
        lines.append(f"audit: {'clean' if not v else f'{len(v)} violation(s)'}")
        return "\n".join(lines)
    finally:
        c.close()


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "setup":
        print(f"grant: {app.setup(app.open_fence())}")
    elif cmd == "revoke":
        print(revoke())
    elif cmd == "regrant":
        print(regrant())
    elif cmd == "show":
        print(show())
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv)
