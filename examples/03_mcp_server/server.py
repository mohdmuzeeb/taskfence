"""Refund desk: an MCP server whose long-running refunds are fenced by TaskFence.

`start_refund` admits a task (issues a token bound to the grant's current epoch)
and returns at once. The work runs in the background; when it finishes, the
refund commits only if `guard()` admits it inside the commit transaction. If the
user revoked access in the meantime (`python admin.py revoke`, a separate
process), the refund is blocked.

This uses plain tools plus polling because the Python MCP SDK does not ship the
MCP Tasks extension yet. Authentication of the MCP client is out of scope here:
the principal is fixed by REFUND_DESK_USER.

Run:  python server.py            (stdio transport)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import app  # noqa: E402
from taskfence import Denied, Duplicate, Retry  # noqa: E402
from taskfence.stores.sqlite import connect  # noqa: E402

WORK_SECONDS = float(os.environ.get("REFUND_DESK_WORK_SECONDS", "3"))

fence = app.open_fence()
server = MCPServer("taskfence-refund-desk", version="0.1.0",
                   instructions="Issue refunds for the signed-in user's orders. Refunds run as background "
                                "tasks: call start_refund, then poll refund_status.")
_background: set[asyncio.Task] = set()


class RefundTask(TypedDict, total=False):
    """Tool result. Typed so the server publishes an output schema and structured content."""
    task_id: str
    order_id: str
    amount_cents: int
    status: str            # working | committed | denied | failed | rejected | unknown
    code: str | None       # TaskFence denial code, e.g. STALE_EPOCH, TASK_CANCELLED
    reason: str
    cancelled: bool


async def _run(token: str, task_id: str, order_id: str, amount_cents: int) -> None:
    await asyncio.sleep(WORK_SECONDS)      # stands in for fraud checks, LLM review, waiting on a human ...
    try:
        await asyncio.to_thread(app.commit_refund, fence, token, task_id, order_id, amount_cents)
        status, code = "committed", None
    except Denied as e:
        status, code = "denied", str(e.code)
    except Duplicate:
        status, code = "committed", "DUPLICATE"
    except Retry as e:
        status, code = "failed", f"RETRY: {e}"
    await asyncio.to_thread(app.set_status, task_id, status, code)


@server.tool()
async def start_refund(order_id: str, amount_cents: int) -> RefundTask:
    """Start a refund for one of the user's orders. Returns a task_id; poll refund_status for the result."""
    if amount_cents <= 0:
        return {"status": "rejected", "reason": "amount_cents must be positive"}
    task_id = "rt_" + secrets.token_hex(6)
    c = connect(app.DB)
    try:
        token = fence.issue(c, app.grant_id(), task_id=task_id, ttl_s=3600)
    except Denied as e:
        return {"status": "denied", "code": str(e.code), "reason": "the user has not granted refund access"}
    finally:
        c.close()
    c = connect(app.DB)
    try:
        c.execute("INSERT INTO refund_tasks (task_id, order_id, amount_cents, status) VALUES (?, ?, ?, 'working')",
                  (task_id, order_id, amount_cents))
    finally:
        c.close()
    t = asyncio.create_task(_run(token, task_id, order_id, amount_cents))
    _background.add(t)
    t.add_done_callback(_background.discard)
    return {"task_id": task_id, "status": "working"}


@server.tool()
async def refund_status(task_id: str) -> RefundTask:
    """Get the status of a refund task: working, committed, denied (with a code) or failed."""
    c = connect(app.DB)
    try:
        r = c.execute("SELECT task_id, order_id, amount_cents, status, code FROM refund_tasks WHERE task_id = ?",
                      (task_id,)).fetchone()
    finally:
        c.close()
    if r is None:
        return {"task_id": task_id, "status": "unknown"}
    return {"task_id": r[0], "order_id": r[1], "amount_cents": r[2], "status": r[3], "code": r[4]}


@server.tool()
async def cancel_refund(task_id: str) -> RefundTask:
    """Cancel a refund task. Once this returns, the task can no longer commit."""
    c = connect(app.DB)
    try:
        fence.cancel_task(c, task_id)
    finally:
        c.close()
    return {"task_id": task_id, "cancelled": True}


if __name__ == "__main__":
    app.setup(fence)
    server.run()
