"""Re-project the full Neo4j graph into PostGIS."""
from __future__ import annotations

import json
import sys

from db import get_db
from repositories.campus_repo import CampusRepository
from services.postgis_service import PostGISService
from services.space_sync import build_space_sync_payload


def _count(label: str, items) -> int:
    n = len(items)
    print(f"[resync] {label}: {n}", flush=True)
    return n


def _maybe_json(value):
    """Neo4j stores list/object props (polygon, tags, metadata) as JSON
    strings; parse them back so the payload builder gets real structures."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _sync_space_row(pg, campus_repo, row) -> None:
    """Upsert one Space into PostGIS from a graph row carrying the resolved
    floor_id / building_id / campus_id / organization_id / floor_index."""
    space = dict(row.get("s") or {})
    space["floor_id"] = row.get("floor_id")
    space["building_id"] = row.get("building_id")
    space["campus_id"] = row.get("campus_id") or space.get("campus_id")
    space["organization_id"] = row.get("organization_id")
    space["floor_index"] = row.get("floor_index")
    space["polygon"] = _maybe_json(space.get("polygon"))
    space["polygon_global"] = _maybe_json(space.get("polygon_global"))
    space["tags"] = _maybe_json(space.get("tags")) or []
    space["metadata"] = _maybe_json(space.get("metadata")) or {}
    pg._apply_sync_space(build_space_sync_payload(space, campus_repo))


def main() -> int:
    campus_id = sys.argv[1] if len(sys.argv) > 1 else None

    pg = PostGISService()
    if pg.engine is None:
        print(
            "[resync] PostGIS not configured (SUPABASE_DB_URL unset). Abort.",
            file=sys.stderr,
        )
        return 2

    db = get_db()
    campus_repo = CampusRepository(db)  # used for per-space georef resolution
    p = {"campus_id": campus_id}  # campus_id may be None => "all campuses"
    print(f"[resync] target: {campus_id or 'ALL campuses'}", flush=True)

    ok = err = 0

    # 1. Organizations that own the target campus (or all).
    orgs = db.execute(
        """
        MATCH (o:Organization)
        OPTIONAL MATCH (o)-[:HAS_CAMPUS]->(c:Campus)
        WITH o, collect(c.id) AS cids
        WHERE $campus_id IS NULL OR $campus_id IN cids
        RETURN o.id AS id, o.name AS name,
               o.entity_type AS entity_type, o.description AS description
        """,
        p,
    )
    for o in orgs:
        if not o.get("id"):
            continue
        try:
            pg._apply_sync_organization({
                "id": o["id"],
                "name": o.get("name") or o["id"],
                "entity_type": o.get("entity_type"),
                "description": o.get("description"),
            })
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] org {o.get('id')!r} failed: {exc}", flush=True)
    _count("organizations", orgs)

    # 2. Campuses.
    campuses = db.execute(
        """
        MATCH (c:Campus)
        WHERE $campus_id IS NULL OR c.id = $campus_id
        OPTIONAL MATCH (o:Organization)-[:HAS_CAMPUS]->(c)
        RETURN c.id AS id, coalesce(c.organization_id, o.id) AS organization_id,
               c.name AS name, c.description AS description,
               coalesce(c.is_public, false) AS is_public
        """,
        p,
    )
    for c in campuses:
        try:
            pg._apply_sync_campus({
                "id": c["id"],
                "organization_id": c.get("organization_id"),
                "name": c.get("name") or c["id"],
                "description": c.get("description"),
                "is_public": c.get("is_public", False),
            })
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] campus {c.get('id')!r} failed: {exc}", flush=True)
    _count("campuses", campuses)

    # 3. Buildings.
    buildings = db.execute(
        """
        MATCH (c:Campus)-[:HAS_BUILDING]->(b:Building)
        WHERE $campus_id IS NULL OR c.id = $campus_id
        OPTIONAL MATCH (o:Organization)-[:HAS_CAMPUS]->(c)
        RETURN b.id AS id, c.id AS campus_id,
               coalesce(b.organization_id, o.id) AS organization_id,
               b.name AS name, b.short_name AS short_name, b.address AS address,
               b.origin_lat AS origin_lat, b.origin_lng AS origin_lng,
               b.origin_bearing AS origin_bearing, b.floor_count AS floor_count,
               coalesce(b.is_public, false) AS is_public
        """,
        p,
    )
    for b in buildings:
        try:
            pg._apply_sync_building({
                "id": b["id"],
                "campus_id": b.get("campus_id"),
                "organization_id": b.get("organization_id"),
                "name": b.get("name") or b["id"],
                "short_name": b.get("short_name"),
                "address": b.get("address"),
                "origin_lat": b.get("origin_lat"),
                "origin_lng": b.get("origin_lng"),
                "origin_bearing": b.get("origin_bearing"),
                "floor_count": b.get("floor_count"),
                "is_public": b.get("is_public", False),
            })
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] building {b.get('id')!r} failed: {exc}", flush=True)
    _count("buildings", buildings)

    # 4. Floors.
    floors = db.execute(
        """
        MATCH (c:Campus)-[:HAS_BUILDING]->(b:Building)-[:HAS_FLOOR]->(f:Floor)
        WHERE $campus_id IS NULL OR c.id = $campus_id
        OPTIONAL MATCH (o:Organization)-[:HAS_CAMPUS]->(c)
        RETURN f.id AS floor_id, b.id AS building_id, c.id AS campus_id,
               coalesce(b.organization_id, o.id) AS organization_id,
               f.floor_index AS floor_index, f.display_name AS display_name,
               f.floor_plan_url AS floor_plan_url, f.floor_plan_scale AS floor_plan_scale,
               f.floor_plan_origin_x AS floor_plan_origin_x,
               f.floor_plan_origin_y AS floor_plan_origin_y,
               f.floor_plan_bounds AS floor_plan_bounds
        """,
        p,
    )
    for f in floors:
        try:
            pg._apply_sync_floor({
                "id": f"{f.get('building_id')}_{f.get('floor_id')}",
                "organization_id": f.get("organization_id"),
                "campus_id": f.get("campus_id"),
                "building_id": f.get("building_id"),
                "floor_id": f.get("floor_id"),
                "floor_index": f.get("floor_index"),
                "display_name": f.get("display_name"),
                "floor_plan_url": f.get("floor_plan_url"),
                "floor_plan_scale": f.get("floor_plan_scale"),
                "floor_plan_origin_x": f.get("floor_plan_origin_x"),
                "floor_plan_origin_y": f.get("floor_plan_origin_y"),
                "floor_plan_bounds": f.get("floor_plan_bounds"),
                "is_public": True,
            })
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] floor {f.get('floor_id')!r} failed: {exc}", flush=True)
    _count("floors", floors)

    # 5. Spaces, with floor_id / building_id / campus_id / organization_id
    #    resolved from the graph relationships (these are often missing as
    #    bare node properties, which is exactly the gap in PostGIS).
    spaces = db.execute(
        """
        MATCH (b:Building)-[:HAS_FLOOR]->(f:Floor)-[:HAS_SPACE]->(s:Space)
        OPTIONAL MATCH (c:Campus)-[:HAS_BUILDING]->(b)
        WHERE $campus_id IS NULL OR c.id = $campus_id
        OPTIONAL MATCH (o:Organization)-[:HAS_CAMPUS]->(c)
        RETURN s AS s, f.id AS floor_id, b.id AS building_id, c.id AS campus_id,
               coalesce(b.organization_id, o.id) AS organization_id,
               f.floor_index AS floor_index
        """,
        p,
    )
    for row in spaces:
        try:
            _sync_space_row(pg, campus_repo, row)
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] space {(row.get('s') or {}).get('id')!r} failed: {exc}", flush=True)
    _count("floor-attached spaces", spaces)

    # 5b. Connector spaces (doors, passages, stairs, lifts) are standalone in
    #     Neo4j — linked only by CONNECTS_TO, never under a Floor — so the
    #     query above can't reach them and they keep null floor_id/org. Inherit
    #     those from a floored neighbour one or two hops away.
    orphans = db.execute(
        """
        MATCH (s:Space)
        WHERE ($campus_id IS NULL OR s.campus_id = $campus_id)
          AND NOT ( (:Floor)-[:HAS_SPACE]->(s) )
        MATCH (s)-[:CONNECTS_TO*1..2]-(n:Space)<-[:HAS_SPACE]-(f:Floor)<-[:HAS_FLOOR]-(b:Building)
        OPTIONAL MATCH (c:Campus)-[:HAS_BUILDING]->(b)
        OPTIONAL MATCH (o:Organization)-[:HAS_CAMPUS]->(c)
        WITH s, collect({
            floor_id: f.id, building_id: b.id, campus_id: c.id,
            org: coalesce(b.organization_id, o.id), fidx: f.floor_index
        })[0] AS g
        RETURN s AS s, g.floor_id AS floor_id, g.building_id AS building_id,
               g.campus_id AS campus_id, g.org AS organization_id,
               g.fidx AS floor_index
        """,
        p,
    )
    for row in orphans:
        try:
            _sync_space_row(pg, campus_repo, row)
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] connector {(row.get('s') or {}).get('id')!r} failed: {exc}", flush=True)
    _count("connector spaces", orphans)

    # 6. CONNECTS_TO edges -> space_connections (one direct row per edge, so
    #    the room->door->corridor adjacency the assistant relies on is mirrored).
    edges = db.execute(
        """
        MATCH (a:Space)-[r:CONNECTS_TO]->(b:Space)
        WHERE $campus_id IS NULL OR a.campus_id = $campus_id
        RETURN a.id AS from_id, b.id AS to_id,
               r.connection_type AS ctype,
               coalesce(a.is_accessible, true) AS acc
        """,
        p,
    )
    for e in edges:
        if not e.get("from_id") or not e.get("to_id"):
            continue
        try:
            pg._apply_sync_direct_edge(
                e["from_id"], e["to_id"],
                connection_type=e.get("ctype"),
                is_accessible=bool(e.get("acc", True)),
            )
            ok += 1
        except Exception as exc:
            err += 1
            print(f"[resync] edge {e.get('from_id')}->{e.get('to_id')} failed: {exc}", flush=True)
    _count("connections", edges)

    print(f"[resync] done — applied {ok}, failed {err}", flush=True)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
