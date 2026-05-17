"""Durable retry queue for Neo4j -> PostGIS synchronisation.

Every PostGIS mutation that used to run inline now lands in a `:SyncOutbox`
node. A background worker drains the queue, calls the matching
`PostGISService._apply_*` method, deletes the row on success, and applies
exponential backoff on failure. Neo4j stays the source of truth; PostGIS
catches up at its own pace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from core.config import settings
from core.request_context import current_is_service, current_org_id
from db import Database, get_db

logger = logging.getLogger(__name__)


_LAST_ERROR_MAX_LEN = 500
_BACKOFF_CAP_SHIFTS = 6  # cap exponential growth at 2**6 = 64x base


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(op: str, payload: dict[str, Any]) -> bool:
    """Persist a sync operation to the Neo4j outbox.

    Called from the public `PostGISService.sync_*` facades right after the
    Neo4j write has committed. Returns False (no-op) when sync is disabled
    so the legacy "supabase_enable_sync=false" path stays a true no-op.
    """
    if not settings.supabase_enable_sync:
        return False
    try:
        get_db().execute_write(
            """
            CREATE (o:SyncOutbox {
                id: $id,
                op: $op,
                payload: $payload,
                created_at: $created_at,
                attempts: 0,
                next_attempt_at: $created_at,
                last_error: null
            })
            """,
            {
                "id": str(uuid.uuid4()),
                "op": op,
                "payload": json.dumps(payload, default=str),
                "created_at": _utcnow_iso(),
            },
        )
        return True
    except Exception as exc:
        # Enqueue itself failed (Neo4j unavailable). The caller's data write
        # already committed, so we surface this loudly rather than silently
        # dropping the change.
        logger.error("sync_outbox enqueue(%s) failed: %s", op, exc)
        return False


def outbox_status() -> dict[str, Any]:
    """Lightweight snapshot for the admin endpoint."""
    rows = get_db().execute(
        """
        MATCH (o:SyncOutbox)
        RETURN count(o) AS pending,
               min(o.created_at) AS oldest_created_at,
               max(o.attempts) AS max_attempts
        """
    )
    if not rows:
        return {"pending": 0, "oldest_created_at": None, "max_attempts": 0}
    row = rows[0]
    return {
        "pending": row.get("pending", 0) or 0,
        "oldest_created_at": row.get("oldest_created_at"),
        "max_attempts": row.get("max_attempts", 0) or 0,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Built lazily so we don't import PostGISService at module load time
# (the SQLAlchemy engine + table metadata is heavy and we don't want
# a circular import when sync is disabled).
_DISPATCH: dict[str, Callable[[Any, dict[str, Any]], Any]] | None = None


def _build_dispatch() -> dict[str, Callable[[Any, dict[str, Any]], Any]]:
    from services.postgis_service import PostGISService  # local to dodge cycles

    def call(method_name: str):
        def _invoke(svc: PostGISService, payload: dict[str, Any]):
            return getattr(svc, method_name)(**payload)
        return _invoke

    return {
        "sync_organization": call("_apply_sync_organization"),
        "delete_organization": call("_apply_delete_organization"),
        "delete_organization_cascade": call("_apply_delete_organization_cascade"),
        "sync_campus": call("_apply_sync_campus"),
        "delete_campus": call("_apply_delete_campus"),
        "delete_campus_cascade": call("_apply_delete_campus_cascade"),
        "sync_building": call("_apply_sync_building"),
        "delete_building": call("_apply_delete_building"),
        "delete_building_cascade": call("_apply_delete_building_cascade"),
        "sync_space": call("_apply_sync_space"),
        "sync_space_geometry": call("_apply_sync_space_geometry"),
        "delete_space": call("_apply_delete_space"),
        "sync_floor": call("_apply_sync_floor"),
        "delete_floor": call("_apply_delete_floor"),
        "delete_floor_cascade": call("_apply_delete_floor_cascade"),
        "sync_connection": call("_apply_sync_connection"),
        "delete_connection_group": call("_apply_delete_connection_group"),
        "sync_direct_edge": call("_apply_sync_direct_edge"),
        "delete_edges_for_space": call("_apply_delete_edges_for_space"),
        "update_connection_group_access": call("_apply_update_connection_group_access"),
        "sync_import": call("_apply_sync_import"),
    }


def _get_dispatch() -> dict[str, Callable[[Any, dict[str, Any]], Any]]:
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = _build_dispatch()
    return _DISPATCH


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class OutboxWorker:
    """Polls the outbox and drains it.

    Single-instance, started in the FastAPI lifespan. Drain order is
    `created_at ASC, id ASC` so dependent ops (campus -> building -> floor
    -> space) replay in the order they were enqueued.
    """

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def db(self) -> Database:
        return self._db or get_db()

    def start(self) -> None:
        if not settings.supabase_enable_sync:
            logger.info("sync_outbox: disabled (supabase_enable_sync=false)")
            return
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="sync-outbox-worker")
        logger.info("sync_outbox: worker started (poll=%.1fs)",
                    settings.sync_outbox_poll_interval_s)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("sync_outbox: worker stopped")

    async def _run(self) -> None:
        # Lazily import to avoid pulling SQLAlchemy + creating engines until
        # we actually have something to process. PostGISService is a thin
        # singleton-backed wrapper so reusing one instance is fine.
        from services.postgis_service import PostGISService

        service = PostGISService()
        while not self._stopped.is_set():
            try:
                processed = await asyncio.to_thread(self._drain_once, service)
            except Exception as exc:
                # Never let the loop die — that would silently stop sync.
                logger.exception("sync_outbox: drain loop error: %s", exc)
                processed = 0

            # If we just drained a full batch, loop again immediately;
            # there's likely more work.
            if processed >= settings.sync_outbox_batch_size:
                continue
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=settings.sync_outbox_poll_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    def _drain_once(self, service) -> int:
        rows = self.db.execute(
            """
            MATCH (o:SyncOutbox)
            WHERE o.next_attempt_at <= $now
            RETURN o.id AS id, o.op AS op, o.payload AS payload,
                   o.attempts AS attempts
            ORDER BY o.created_at ASC, o.id ASC
            LIMIT $limit
            """,
            {"now": _utcnow_iso(), "limit": settings.sync_outbox_batch_size},
        )
        if not rows:
            return 0

        dispatch = _get_dispatch()
        # Background work runs in service mode so PostGIS RLS doesn't reject
        # writes for ops enqueued under request scopes that have since ended.
        token = current_is_service.set(True)
        org_token = current_org_id.set(None)
        try:
            for row in rows:
                self._process(service, dispatch, row)
        finally:
            current_is_service.reset(token)
            current_org_id.reset(org_token)
        return len(rows)

    def _process(self, service, dispatch, row: dict[str, Any]) -> None:
        outbox_id = row["id"]
        op = row["op"]
        attempts = row.get("attempts") or 0

        handler = dispatch.get(op)
        if handler is None:
            self._mark_failure(outbox_id, attempts, f"unknown op: {op}")
            return

        try:
            payload = json.loads(row["payload"]) if row.get("payload") else {}
        except Exception as exc:
            self._mark_failure(outbox_id, attempts, f"bad payload: {exc}")
            return

        try:
            handler(service, payload)
        except Exception as exc:
            self._mark_failure(outbox_id, attempts, str(exc))
            return

        self._delete(outbox_id)

    def _delete(self, outbox_id: str) -> None:
        self.db.execute_write(
            "MATCH (o:SyncOutbox {id: $id}) DELETE o",
            {"id": outbox_id},
        )

    def _mark_failure(self, outbox_id: str, attempts: int, error: str) -> None:
        new_attempts = attempts + 1
        max_attempts = settings.sync_outbox_max_attempts
        if max_attempts and new_attempts >= max_attempts:
            logger.error(
                "sync_outbox: giving up on %s after %d attempts: %s",
                outbox_id, new_attempts, error,
            )
            self._delete(outbox_id)
            return

        shift = min(new_attempts, _BACKOFF_CAP_SHIFTS)
        delay = settings.sync_outbox_base_backoff_s * (2 ** shift)
        next_at = datetime.now(timezone.utc).timestamp() + delay
        next_iso = datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat()

        truncated = (error or "")[:_LAST_ERROR_MAX_LEN]
        self.db.execute_write(
            """
            MATCH (o:SyncOutbox {id: $id})
            SET o.attempts = $attempts,
                o.next_attempt_at = $next_attempt_at,
                o.last_error = $last_error
            """,
            {
                "id": outbox_id,
                "attempts": new_attempts,
                "next_attempt_at": next_iso,
                "last_error": truncated,
            },
        )
        if new_attempts % 5 == 0:
            logger.warning(
                "sync_outbox: %s has failed %d times: %s",
                outbox_id, new_attempts, truncated,
            )


_worker: OutboxWorker | None = None


def get_worker() -> OutboxWorker:
    global _worker
    if _worker is None:
        _worker = OutboxWorker()
    return _worker
