"""Backfill embeddings for spaces that don't have one in either DB."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx
from sqlalchemy import text

from db import get_db
from services.postgis_service import PostGISService


def _assistant_url() -> str:
    return os.getenv("ASSISTANT_URL", "http://assistant:8001").rstrip("/")


def _text_for(space: dict[str, Any]) -> str:
    """Mirror the import-time template
    (services/import_service.py: '{name}. Type: {type}. Tags: {tags}')
    so a re-embedded space sits in the same vector space as one that
    was embedded at import."""
    name = space.get("display_name") or ""
    space_type = space.get("space_type") or ""
    tags = space.get("tags") or []
    if isinstance(tags, str):
        tags_str = tags
    else:
        tags_str = " ".join(str(t) for t in tags)
    return f"{name}. Type: {space_type}. Tags: {tags_str}"


def _embed_batch(texts: list[str], timeout: float = 60.0) -> list[list[float]]:
    resp = httpx.post(
        f"{_assistant_url()}/internal/embed",
        json={"texts": texts},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("vectors") or []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--campus", default=None,
                   help="restrict to one campus_id (default: all)")
    p.add_argument("--batch", type=int, default=32,
                   help="texts per /internal/embed round-trip (default: 32)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be embedded, don't write")
    args = p.parse_args(argv)

    db = get_db()
    pg = PostGISService()
    if pg.engine is None:
        print(
            "[backfill] PostGIS is not configured "
            "(SUPABASE_DB_URL unset). Aborting.",
            file=sys.stderr,
        )
        return 2

    where = "s.embedding IS NULL"
    params: dict[str, Any] = {}
    if args.campus:
        where += " AND s.campus_id = $campus_id"
        params["campus_id"] = args.campus

    print(f"[backfill] scanning Neo4j for spaces with NULL embedding "
          f"(campus={args.campus or 'all'})...", flush=True)
    rows = db.execute(
        f"""
        MATCH (s:Space)
        WHERE {where}
        RETURN s.id AS id,
               s.display_name AS display_name,
               s.space_type AS space_type,
               s.tags AS tags
        """,
        params,
    )
    total = len(rows)
    print(f"[backfill] {total} spaces need an embedding", flush=True)
    if total == 0:
        return 0

    if args.dry_run:
        for r in rows[:10]:
            print(f"[backfill] would embed {r['id']!r}: {_text_for(r)!r}")
        if total > 10:
            print(f"[backfill] (+ {total - 10} more)")
        return 0

    written = 0
    failed = 0
    for batch_start in range(0, total, args.batch):
        batch = rows[batch_start:batch_start + args.batch]
        texts = [_text_for(r) for r in batch]

        try:
            vectors = _embed_batch(texts)
        except Exception as exc:
            failed += len(batch)
            print(f"[backfill] embed batch failed: {exc}", file=sys.stderr)
            continue

        if len(vectors) != len(batch):
            failed += len(batch)
            print(
                f"[backfill] batch size mismatch: requested {len(batch)} "
                f"got {len(vectors)}",
                file=sys.stderr,
            )
            continue

        # Write Neo4j first (it's the authoritative store), then mirror
        # to PostGIS - same ordering as the live update_space path.
        for r, vec in zip(batch, vectors):
            try:
                db.execute_write(
                    "MATCH (s:Space {id: $id}) SET s.embedding = $vec",
                    {"id": r["id"], "vec": vec},
                )
                lit = "[" + ",".join(f"{float(v):.7f}" for v in vec) + "]"
                with pg.engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE building_spaces "
                            "SET embedding = CAST(:e AS vector) "
                            "WHERE id = :id"
                        ),
                        {"e": lit, "id": r["id"]},
                    )
                written += 1
            except Exception as exc:
                failed += 1
                print(
                    f"[backfill] failed to write {r['id']!r}: {exc}",
                    file=sys.stderr,
                )

        print(
            f"[backfill] {written}/{total} written, {failed} failed",
            flush=True,
        )

    print(
        f"[backfill] done - wrote {written}, failed {failed}, "
        f"total {total}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
