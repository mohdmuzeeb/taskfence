"""Deterministic race scheduling with latches, not sleeps.

A worker that reaches a paused hook point blocks until the test releases it.
Timeouts only turn a hung schedule into a test failure.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .. import _hooks

DEFAULT_TIMEOUT_S = 10.0


class ScheduleTimeout(AssertionError):
    pass


class Pause:
    def __init__(self, point: str, match: Callable[[dict[str, Any]], bool] | None):
        self.point = point
        self.match = match
        self.ctx: dict[str, Any] = {}
        self._reached = threading.Event()
        self._go = threading.Event()
        self._consumed = False

    def wait_reached(self, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        if not self._reached.wait(timeout):
            raise ScheduleTimeout(f"no worker reached {self.point!r} within {timeout}s")
        return self.ctx

    def release(self) -> None:
        self._go.set()

    @property
    def reached(self) -> bool:
        return self._reached.is_set()


class Latches:
    """Install with `with Latches() as l:`; arrange pauses or faults at hook points.

    Points inside the library are in `taskfence._hooks.GUARD_POINTS`. Application
    code may add its own with `taskfence.testing.hook("app.whatever")`.
    """

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._pauses: list[Pause] = []
        self._faults: dict[str, list[tuple[BaseException, Callable[[dict[str, Any]], bool] | None]]] = {}
        self.trace: list[tuple[str, dict[str, Any]]] = []

    def pause_at(self, point: str, *, match: Callable[[dict[str, Any]], bool] | None = None) -> Pause:
        """Pause the first worker that reaches `point` (and satisfies `match`, if given)."""
        p = Pause(point, match)
        with self._lock:
            self._pauses.append(p)
        return p

    def fail_at(self, point: str, exc: BaseException, *,
                match: Callable[[dict[str, Any]], bool] | None = None) -> None:
        """Raise `exc` in the first worker that reaches `point`."""
        with self._lock:
            self._faults.setdefault(point, []).append((exc, match))

    def arrive(self, point: str, ctx: dict[str, Any]) -> None:
        with self._lock:
            self.trace.append((point, dict(ctx)))
            pause = next((p for p in self._pauses if p.point == point and not p._consumed
                          and (p.match is None or p.match(ctx))), None)
            if pause is not None:
                pause._consumed = True
            faults = self._faults.get(point, [])
            fault = next(((e, m) for e, m in faults if m is None or m(ctx)), None)
            if fault is not None:
                faults.remove(fault)
        if fault is not None:
            raise fault[0]
        if pause is not None:
            pause.ctx = dict(ctx)
            pause._reached.set()
            if not pause._go.wait(self.timeout_s):
                raise ScheduleTimeout(f"{point!r} was never released")

    def release_all(self) -> None:
        with self._lock:
            for p in self._pauses:
                p.release()

    def __enter__(self) -> "Latches":
        _hooks.install(self)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release_all()
        _hooks.install(None)


@contextmanager
def latches(timeout_s: float = DEFAULT_TIMEOUT_S) -> Iterator[Latches]:
    with Latches(timeout_s) as l:
        yield l


def run_in_thread(fn: Callable[[], Any]) -> "Worker":
    w = Worker(fn)
    w.start()
    return w


class Worker(threading.Thread):
    """Thread that records its return value or exception for the test to inspect."""

    def __init__(self, fn: Callable[[], Any]):
        super().__init__(daemon=True)
        self.fn = fn
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.fn()
        except BaseException as e:      # noqa: BLE001 - surfaced to the test
            self.error = e

    def outcome(self, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
        """Join, then return the result or re-raise the worker's exception."""
        self.join(timeout)
        if self.is_alive():
            raise ScheduleTimeout("worker did not finish")
        if self.error is not None:
            raise self.error
        return self.result
