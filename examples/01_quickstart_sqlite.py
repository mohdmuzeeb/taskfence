"""TaskFence quickstart: a long-running refund task whose permission is revoked mid-task.

Run:  python examples/01_quickstart_sqlite.py
"""

import tempfile
import threading
from pathlib import Path

from taskfence import Denied, Fence, KeyRing
from taskfence.stores.sqlite import connect

db = Path(tempfile.mkdtemp()) / "app.sqlite"
fence = Fence(KeyRing.generate())          # production: KeyRing.from_env("TASKFENCE_KEYS")

admin = connect(db)
fence.migrate(admin)
admin.executescript("CREATE TABLE orders (id TEXT PRIMARY KEY, refunded INTEGER NOT NULL DEFAULT 0);"
                    "INSERT INTO orders VALUES ('order:9001', 0);")

# 1. The user grants the agent permission, and a task is admitted with a token.
g = fence.grant(admin, principal="user:42", operation="refund.issue", resources=["order:9001"])
token = fence.issue(admin, g.grant_id, task_id="task-1")

work_done = threading.Event()
revoked = threading.Event()


def agent_task():
    conn = connect(db)
    work_done.set()
    revoked.wait()                       # ... minutes of LLM and tool calls ...
    try:
        with fence.transaction(conn):    # 3. the effect and the check commit together, or not at all
            fence.guard(conn, token, operation="refund.issue", resource="order:9001",
                        operation_id="task-1:refund:order:9001")
            conn.execute("UPDATE orders SET refunded = 1 WHERE id = 'order:9001'")
        print("refund committed")
    except Denied as e:
        print(f"refund blocked: {e.code}")


t = threading.Thread(target=agent_task)
t.start()
work_done.wait()

# 2. While the task is working, the user revokes permission.
r = fence.revoke(admin, g.grant_id)
print(f"revocation effective: {r.effective}")
revoked.set()
t.join()

print("order refunded:", bool(admin.execute("SELECT refunded FROM orders").fetchone()[0]))
