"""Named hook points inside the library. No-ops unless a test installs a controller.

Kept separate from `taskfence.testing` so the core never imports test code.
"""

from __future__ import annotations

from typing import Any, Protocol

GUARD_POINTS = (
    "guard.before_fence",   # caller's transaction open, fence row not yet locked
    "guard.after_fence",    # fence row locked, every check passed, op row not yet inserted
    "guard.after_insert",   # op row inserted; guard() is about to return
)


class Controller(Protocol):
    def arrive(self, point: str, ctx: dict[str, Any]) -> None: ...


_controller: Controller | None = None


def install(c: Controller | None) -> None:
    global _controller
    _controller = c


def hook(point: str, **ctx: Any) -> None:
    c = _controller
    if c is not None:
        c.arrive(point, ctx)
