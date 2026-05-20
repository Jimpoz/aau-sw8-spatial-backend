"""One-shot script: reconcile Neo4j ``Landmark`` nodes into the
``landmarks`` table in PostGIS."""
from __future__ import annotations

import sys

from db import get_db
from services.postgis_service import PostGISService


def main() -> int:
    pg = PostGISService()
    if pg.engine is None:
        print(
            "[backfill_landmarks] PostGIS is not configured "
            "(SUPABASE_DB_URL unset or SUPABASE_ENABLE_SYNC=false). "
            "Nothing to do.",
            file=sys.stderr,
        )
        return 2

    db = get_db()
    print("[backfill_landmarks] fetching Landmark nodes from Neo4j ...", flush=True)

    rows = db.execute(
        """
        MATCH (l:Landmark)
        OPTIONAL MATCH (s:Space {id: l.space_id})
        OPTIONAL MATCH (s)<-[:HAS_SPACE]-(f:Floor)
        OPTIONAL MATCH (f)<-[:HAS_FLOOR]-(b:Building)
        OPTIONAL MATCH (b)<-[:HAS_BUILDING]-(c:Campus)
        OPTIONAL MATCH (c)<-[:HAS_CAMPUS]-(o:Organization)
        RETURN l.id              AS id,
               l.name            AS name,
               l.space_id        AS space_id,
               coalesce(l.floor_id, f.id)               AS floor_id,
               coalesce(l.building_id, b.id)            AS building_id,
               coalesce(l.campus_id, c.id)              AS campus_id,
               coalesce(l.organization_id, o.id,
                        s.organization_id)              AS organization_id,
               l.image_b64       AS image_b64,
               l.image_width     AS image_width,
               l.image_height    AS image_height,
               l.created_by      AS created_by,
               l.created_at      AS created_at
        """
    )
    total = len(rows)
    print(f"[backfill_landmarks] found {total} landmark node(s)", flush=True)
    if total == 0:
        return 0

    written = 0
    skipped = 0
    for row in rows:
        payload = dict(row)
        if not payload.get("id"):
            skipped += 1
            continue
        try:
            pg._apply_sync_landmark(payload)
            written += 1
        except Exception as exc:
            print(
                f"[backfill_landmarks] failed for {payload.get('id')!r}: {exc}",
                file=sys.stderr,
            )
            skipped += 1

    print(
        f"[backfill_landmarks] done. written={written} skipped={skipped} total={total}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
