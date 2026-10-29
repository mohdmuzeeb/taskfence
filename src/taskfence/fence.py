"""The `Fence` facade: grants, revocation, tokens and the commit-time guard."""

from __future__ import annotations

import functools
import secrets
import time
from contextlib import AbstractContextManager
from typing import Any, Callable, TypeVar

from . import _hooks
from .outcomes import Admission, Code, Denied, Grant, Receipt, Revocation, UsageError
from .stores.sqlite import SQLiteStore
from .tokens import Claims, KeyRing, sign, verify

F = TypeVar("F", bound=Callable[..., Any])
Predicate = Callable[[Any, Claims, "str | None"], bool]


class Fence:
    """Commit-time authorization for long-running agent tasks.

    The safety property: once a revocation is effective, no effect admitted under
    a revoked epoch commits. In embedded mode a revocation is effective when
    `revoke()` returns (or when the caller's transaction that ran it commits).
    """

    def __init__(self, keys: KeyRing, store: SQLiteStore | None = None, *,
                 clock: Callable[[], float] = time.time, leeway_s: int = 30):
        self.keys = keys
        self.store = store or SQLiteStore()
        self.clock = clock
        self.leeway_s = leeway_s

    # ------------------------------------------------------------------ setup

    def migrate(self, conn) -> None:
        """Create the taskfence_* tables in the application's database (idempotent)."""
        self.store.migrate(conn)

    def transaction(self, conn) -> AbstractContextManager[None]:
        """Open a write transaction suitable for `guard()`; commits on success, rolls back on error."""
        return self.store.transaction(conn)

    # ------------------------------------------------------------------ control plane (embedded mode)

    def grant(self, conn, *, principal: str, operation: str, resources: list[str],
              grant_id: str | None = None) -> Grant:
        """Create a grant at epoch 1. `resources` may contain "*" for any resource."""
        if not resources:
            raise ValueError("resources must not be empty; use ['*'] to allow any resource")
        gid = grant_id or "g_" + secrets.token_hex(8)
        with self.store.errors(), self.store.owned_or_joined(conn):
            self.store.create_grant(conn, gid, principal, operation, list(resources))
        return Grant(gid, principal, operation, 1, tuple(sorted(resources)))

    def revoke(self, conn, grant_id: str) -> Revocation:
        """Kill every token issued for this grant so far. Idempotent.

        If a guarded transaction for this grant is open, this call waits for it
        (bounded by that transaction, not by the task).
        """
        with self.store.errors(), self.store.owned_or_joined(conn) as owned:
            self._embedded_row(conn, grant_id)
            min_epoch, seq = self.store.revoke(conn, grant_id)
        return Revocation(grant_id, owned, min_epoch, seq)

    def regrant(self, conn, grant_id: str, *, resources: list[str] | None = None) -> Grant:
        """Start a new epoch. Tokens from earlier epochs stay dead."""
        with self.store.errors(), self.store.owned_or_joined(conn):
            f = self._embedded_row(conn, grant_id)
            new_epoch, _ = self.store.regrant(conn, grant_id, resources)   # computed under the lock
            scope = self.store.scope(conn, grant_id)
        return Grant(grant_id, f["principal"], f["operation"], new_epoch, scope)

    def cancel_task(self, conn, task_id: str) -> int:
        """Fence a task: none of its operations can commit afterwards. Returns the commit seq."""
        with self.store.errors(), self.store.owned_or_joined(conn):
            return self.store.cancel_task(conn, task_id)

    def cancel_operation(self, conn, operation_id: str, *, task_id: str) -> bool:
        """Tombstone one operation. Returns False if it had already committed."""
        with self.store.errors(), self.store.owned_or_joined(conn):
            return self.store.cancel_operation(conn, operation_id, task_id)

    def _embedded_row(self, conn, grant_id: str) -> dict[str, Any]:
        f = self.store.fence_row(conn, grant_id)
        if f is None:
            raise KeyError(f"unknown grant {grant_id!r}")
        if f["mode"] != "embedded":
            raise UsageError(f"grant {grant_id!r} is federated; change it through apply_install/apply_fence")
        return f

    # ------------------------------------------------------------------ tokens

    def issue(self, conn, grant_id: str, *, task_id: str, ttl_s: int = 3600) -> str:
        """Admit a task: return a signed token bound to the grant's current epoch."""
        f = self._embedded_row(conn, grant_id)
        if f["min_epoch"] > f["epoch"]:
            raise Denied(Code.STALE_EPOCH, "grant is revoked")
        return self.mint(grant_id=grant_id, principal=f["principal"], operation=f["operation"],
                         epoch=f["epoch"], task_id=task_id, ttl_s=ttl_s)

    def mint(self, *, grant_id: str, principal: str, operation: str, epoch: int, task_id: str,
             ttl_s: int = 3600) -> str:
        """Sign a token directly. Used by a grant authority in federated mode."""
        now = int(self.clock())
        return sign(Claims(grant_id, principal, operation, int(epoch), task_id, self.keys.signing_kid,
                           now, now + int(ttl_s)), self.keys)

    def verify(self, token: str) -> Claims:
        return verify(token, self.keys, now=self.clock, leeway_s=self.leeway_s)

    # ------------------------------------------------------------------ the guard

    def guard(self, conn, token: str, *, operation: str, resource: str | None, operation_id: str,
              predicate: Predicate | None = None) -> Admission:
        """Authorize an effect inside the transaction that commits it.

        Call this inside an open transaction, before or after the effect's own
        statements but before COMMIT. It raises `Denied`, `Duplicate` or `Retry`;
        on any of them the caller must roll back. It never commits or rolls back
        by itself.
        """
        self.store.require_transaction(conn)
        claims = self.verify(token)
        if claims.operation != operation:
            raise Denied(Code.OPERATION_MISMATCH, f"token is for {claims.operation!r}")
        ctx = {"task_id": claims.task_id, "operation_id": operation_id}
        _hooks.hook("guard.before_fence", **ctx)
        with self.store.errors():
            code = self.store.lock_and_check_fence(conn, claims.grant_id, claims.principal, operation, claims.epoch)
            if code is not None:
                raise Denied(code)
            if not self.store.in_scope(conn, claims.grant_id, resource):
                raise Denied(Code.RESOURCE_NOT_IN_SCOPE, str(resource))
            if self.store.task_cancelled(conn, claims.task_id):
                raise Denied(Code.TASK_CANCELLED, claims.task_id)
            if predicate is not None and not predicate(conn, claims, resource):
                raise Denied(Code.PREDICATE_FAILED)
            _hooks.hook("guard.after_fence", **ctx)
            seq = self.store.insert_op(conn, operation_id, claims.task_id, claims.grant_id, claims.epoch, resource)
            if isinstance(seq, Code):
                raise Denied(seq, operation_id)
        _hooks.hook("guard.after_insert", **ctx)
        return Admission(operation_id, claims.task_id, claims.grant_id, claims.epoch, resource, seq)

    def fenced(self, *, operation: str, resource: Callable[..., str | None] | str | None,
               operation_id: Callable[..., str], predicate: Predicate | None = None) -> Callable[[F], F]:
        """Decorator: run `fn(conn, **kwargs)` in a transaction that `guard()` admitted.

        The wrapped function is called as `fn(conn, token=..., **kwargs)`; `resource`
        and `operation_id` are callables that receive the same keyword arguments
        (without `token`). The function also receives `admission=`.
        """
        def deco(fn: F) -> F:
            @functools.wraps(fn)
            def wrapper(conn, *, token: str, **kwargs: Any) -> Any:
                res = resource(**kwargs) if callable(resource) else resource
                oid = operation_id(**kwargs)
                with self.transaction(conn):
                    adm = self.guard(conn, token, operation=operation, resource=res, operation_id=oid,
                                     predicate=predicate)
                    return fn(conn, admission=adm, **kwargs)
            return wrapper  # type: ignore[return-value]
        return deco

    # ------------------------------------------------------------------ federated owner side

    def apply_install(self, conn, msg: dict[str, Any]) -> Receipt:
        """INSTALL {msg_id, grant_id, principal, operation, epoch, resources} from a grant authority."""
        return self._apply(conn, {**msg, "kind": "INSTALL"})

    def apply_fence(self, conn, msg: dict[str, Any]) -> Receipt:
        """FENCE {msg_id, grant_id, principal, operation, fence_epoch}: fence_epoch is the new minimum."""
        return self._apply(conn, {**msg, "kind": "FENCE"})

    def _apply(self, conn, msg: dict[str, Any]) -> Receipt:
        if not isinstance(msg.get("msg_id"), str):
            raise ValueError("msg_id is required")
        with self.store.errors(), self.store.owned_or_joined(conn):
            r = self.store.apply_message(conn, msg)
        return Receipt(r["msg_id"], r["kind"], r["grant_id"], r["epoch"], bool(r["applied"]), r["min_epoch"],
                       r["duplicate"])

    # ------------------------------------------------------------------ outbox

    def enqueue(self, conn, operation_id: str, topic: str, payload: Any) -> int:
        """Record an external call to make after commit. Exists iff the guarded transaction commits."""
        return self.store.outbox_enqueue(conn, operation_id, topic, payload)

    def drain_outbox(self, conn, handler: Callable[[dict[str, Any]], None], *, limit: int = 100) -> int:
        """Deliver pending outbox entries at least once. `handler` must be idempotent."""
        n = 0
        for entry in self.store.outbox_pending(conn, limit):
            handler(entry)
            with self.store.owned_or_joined(conn):
                self.store.outbox_mark_delivered(conn, entry["id"])
            n += 1
        return n
