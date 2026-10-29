# Example: an MCP server with revocation-safe background refunds

`server.py` is an MCP server (official Python SDK, `mcp` ≥ 2.3) with three tools:

| Tool | What it does |
|---|---|
| `start_refund(order_id, amount_cents)` | Admits a task (TaskFence token bound to the grant's current epoch) and returns a `task_id` at once. The work runs in the background. |
| `refund_status(task_id)` | `working`, `committed`, or `denied` with a TaskFence code. |
| `cancel_refund(task_id)` | Fences the task: once this returns, it can no longer commit. |

The refund commits only inside a transaction where `fence.guard()` admits it. The guard checks:
- the epoch
- that the order is in the grant's scope
- that the task is not cancelled
- that the order belongs to the user (an application predicate)

The payment itself is written to the outbox in that same transaction.

**Revocation is not an agent tool.** The user revokes from a separate process, `admin.py`, the way a real control plane would.

## Run it

```bash
pip install -e ".[examples]"
```

```bash
python examples/03_mcp_server/demo.py
```

`demo.py` starts the server as a subprocess and drives it with the SDK's `Client` over stdio:

```
1. started rt_…; user revokes while it works -> revoked: effective=True min_epoch=2
   result: denied STALE_EPOCH
   new refund while revoked: denied STALE_EPOCH
2. regranted: epoch=2
   rt_…: committed
3. rt_… cancelled while working: denied TASK_CANCELLED
4. refund on another customer's order: denied RESOURCE_NOT_IN_SCOPE
...
audit: clean
```

To use the server from an MCP host (for example Claude Desktop or Claude Code), first create the database:

```bash
python examples/03_mcp_server/admin.py setup
```

Then register the stdio command `python /path/to/examples/03_mcp_server/server.py`. From another terminal, run `admin.py revoke`, `admin.py regrant` and `admin.py show`.

## What this example does not do
- **No MCP Tasks extension.** The Python SDK does not ship the `io.modelcontextprotocol/tasks` extension yet, so this uses start-then-poll tools. Native Tasks support is planned for v0.2 in TypeScript, built from the TaskFence research adapter.
- **No client authentication.** The principal is fixed by `REFUND_DESK_USER`. A real server must derive it from authenticated MCP auth.
- **Background tasks live in the server process.** If the server restarts, in-flight refunds are lost. They are never half-committed, but they are lost. Their status stays `working`.
- **Synthetic data only.** No payment is sent; the outbox entry is where a relay would pick it up.
