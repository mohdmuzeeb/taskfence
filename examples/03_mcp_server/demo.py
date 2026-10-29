"""End-to-end demo: a real MCP client drives the refund-desk server over stdio.

    python demo.py

1. A refund starts, the user revokes access while it works   -> blocked (STALE_EPOCH)
2. Access is granted again; a new refund runs to completion  -> committed
3. A refund is cancelled while it works                      -> blocked (TASK_CANCELLED)
4. A refund on another customer's order                      -> blocked (RESOURCE_NOT_IN_SCOPE)
Then the auditor checks the database for any post-revocation or post-cancellation commit.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent


async def wait_done(client, task_id: str, timeout_s: float = 30) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        s = await call(client, "refund_status", task_id=task_id)
        if s["status"] != "working":
            return s
        await asyncio.sleep(0.2)
    raise TimeoutError(task_id)


async def call(client, tool: str, **args) -> dict:
    r = await client.call_tool(tool, args)
    if r.is_error:
        raise RuntimeError(f"{tool} failed: {r.content}")
    return r.structured_content


async def run(db: Path, work_seconds: float = 1.5) -> dict[str, dict]:
    os.environ["TASKFENCE_DB"] = str(db)
    sys.path.insert(0, str(HERE))
    import admin  # noqa: E402  (reads TASKFENCE_DB at import)
    import app

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    app.DB = db
    app.setup(app.open_fence())
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], cwd=str(HERE),
                                   env={"TASKFENCE_DB": str(db), "REFUND_DESK_WORK_SECONDS": str(work_seconds),
                                        "PYTHONPATH": os.environ.get("PYTHONPATH", "")})
    out: dict[str, dict] = {}
    async with Client(params) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        print(f"connected; tools: {names}")

        t1 = await call(client, "start_refund", order_id="order:1001", amount_cents=2500)
        print(f"1. started {t1['task_id']}; user revokes while it works -> {admin.revoke()}")
        out["revoked"] = await wait_done(client, t1["task_id"])
        print(f"   result: {out['revoked']['status']} {out['revoked']['code']}")

        out["while_revoked"] = await call(client, "start_refund", order_id="order:1002", amount_cents=100)
        print(f"   new refund while revoked: {out['while_revoked']['status']} {out['while_revoked'].get('code')}")

        print(f"2. {admin.regrant()}")
        t2 = await call(client, "start_refund", order_id="order:1002", amount_cents=1200)
        out["committed"] = await wait_done(client, t2["task_id"])
        print(f"   {t2['task_id']}: {out['committed']['status']}")

        t3 = await call(client, "start_refund", order_id="order:1001", amount_cents=500)
        await call(client, "cancel_refund", task_id=t3["task_id"])
        out["cancelled"] = await wait_done(client, t3["task_id"])
        print(f"3. {t3['task_id']} cancelled while working: {out['cancelled']['status']} {out['cancelled']['code']}")

        t4 = await call(client, "start_refund", order_id="order:2001", amount_cents=100)
        out["not_owner"] = await wait_done(client, t4["task_id"])
        print(f"4. refund on another customer's order: {out['not_owner']['status']} {out['not_owner']['code']}")

    print()
    print(admin.show())
    return out


if __name__ == "__main__":
    asyncio.run(run(Path(tempfile.mkdtemp()) / "refund_desk.sqlite"))
