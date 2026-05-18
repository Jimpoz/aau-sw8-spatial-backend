"""One-time script: copy `Space.embedding` from Neo4j into the new
`building_spaces.embedding` pgvector column.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from db import get_db
from services.postgis_service import PostGISService


_BATCH = 500


def main() -> int:
    pg = PostGISService()
    if pg.engine is None:
        print(
            "[backfill] PostGIS is not configured "
            "(SUPABASE_DB_URL unset or SUPABASE_ENABLE_SYNC=false). "
            "Nothing to do.",
            file=sys.stderr,
        )
        return 2

    print("[backfill] fetching embeddings from Neo4j ...", flush=True)
    rows = get_db().execute(
        "MATCH (s:Space) "
        "WHERE s.embedding IS NOT NULL "
        "RETURN s.id AS id, s.embedding AS embedding"
    )
    total = len(rows)
    print(f"[backfill] found {total} spaces with embeddings", flush=True)
    if total == 0:
        return 0

    written = 0
    skipped = 0
    with pg.engine.begin() as conn:
        for row in rows:
            sid = row.get("id")
            vec = row.get("embedding")
            if not sid or not vec:
                skipped += 1
                continue
            try:
                lit = "[" + ",".join(f"{float(v):.7f}" for v in vec) + "]"
            except (TypeError, ValueError):
                skipped += 1
                continue
            conn.execute(
                text(
                    "UPDATE building_spaces "
                    "SET embedding = CAST(:e AS vector) "
                    "WHERE id = :id"
                ),
                {"e": lit, "id": sid},
            )
            written += 1
            if written % _BATCH == 0:
                print(f"[backfill] wrote {written}/{total} ...", flush=True)

    print(
        f"[backfill] done — wrote {written}, skipped {skipped}, "
        f"total {total}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
