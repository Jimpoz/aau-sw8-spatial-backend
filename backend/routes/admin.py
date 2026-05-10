from fastapi import APIRouter

from services.sync_outbox import outbox_status

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/sync/outbox")
def get_sync_outbox_status() -> dict:
    """Snapshot of the Neo4j -> PostGIS sync queue.

    `pending` is the number of unprocessed entries; `oldest_created_at` is
    the ISO timestamp of the oldest one (None when the queue is empty);
    `max_attempts` is the worst retry count among pending entries.
    """
    return outbox_status()
