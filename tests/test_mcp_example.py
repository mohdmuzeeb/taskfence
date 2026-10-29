"""Runs examples/03_mcp_server/demo.py: a real MCP client and server process over stdio."""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "03_mcp_server"


def test_refund_desk_end_to_end(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    for mod in ("app", "admin", "demo"):
        sys.modules.pop(mod, None)
    import demo

    out = asyncio.run(demo.run(tmp_path / "refund_desk.sqlite", work_seconds=0.5))

    assert (out["revoked"]["status"], out["revoked"]["code"]) == ("denied", "STALE_EPOCH")
    assert (out["while_revoked"]["status"], out["while_revoked"]["code"]) == ("denied", "STALE_EPOCH")
    assert out["committed"]["status"] == "committed"
    assert (out["cancelled"]["status"], out["cancelled"]["code"]) == ("denied", "TASK_CANCELLED")
    assert (out["not_owner"]["status"], out["not_owner"]["code"]) == ("denied", "RESOURCE_NOT_IN_SCOPE")

    import app
    from taskfence.stores.sqlite import connect
    from taskfence.testing import audit
    c = connect(app.DB)
    try:
        assert c.execute("SELECT refunded_cents FROM orders WHERE id = 'order:1001'").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM taskfence_outbox").fetchone()[0] == 1
        assert audit(c) == []
    finally:
        c.close()
