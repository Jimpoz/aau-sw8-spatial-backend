from db import Database
from db_pg import PostgresDatabase
import json
import math

from sqlalchemy import text


def _project_local_to_global(
    local_x: float,
    local_y: float,
    origin_lat: float,
    origin_lng: float,
    bearing_deg: float = 0.0,
    scale: float = 1.0,
) -> tuple[float, float]:
    """Mirror of backend/services/geometry_service.local_to_global_coordinates.
    Inlined here so the assistant container doesn't need to import backend
    code."""
    bearing_rad = math.radians(bearing_deg)
    sx = local_x * scale
    sy = local_y * scale
    rx = sx * math.cos(bearing_rad) - sy * math.sin(bearing_rad)
    ry = sx * math.sin(bearing_rad) + sy * math.cos(bearing_rad)
    lat = origin_lat + ry / 111000.0
    lng = origin_lng + rx / (111000.0 * math.cos(math.radians(origin_lat)))
    return lat, lng


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    EARTH_R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _point_in_polygon(point: tuple[float, float], polygon: list) -> bool:
    """Ray-casting point-in-polygon. point = (lat, lng); polygon is a list
    of [lat, lng] pairs (same shape PostGIS sync writes to polygon_global).
    Lat/lng are treated as planar — fine for room-sized polygons."""
    if not polygon or len(polygon) < 3:
        return False
    px, py = point[0], point[1]
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        try:
            xi, yi = float(polygon[i][0]), float(polygon[i][1])
            xj, yj = float(polygon[j][0]), float(polygon[j][1])
        except (TypeError, ValueError, IndexError):
            j = i
            continue
        if (yi > py) != (yj > py):
            denom = (yj - yi) or 1e-30
            x_intersect = (xj - xi) * (py - yi) / denom + xi
            if px < x_intersect:
                inside = not inside
        j = i
    return inside

class AssistantRepository:
    def __init__(self, db: Database, pg_db: PostgresDatabase | None = None):
        self.db = db
        self.pg_db = pg_db

    def get_campus_name(self, campus_id: str) -> str | None:
        """One-shot Campus name lookup for the "which campus am I in?"
        intent. Kept in the repo (not the service) so the service stays
        ignorant of Cypher."""
        rows = self.db.execute(
            "MATCH (c:Campus {id: $id}) RETURN c.name AS name",
            {"id": campus_id},
        )
        return rows[0].get("name") if rows else None

    def search_similar_spaces(
        self,
        campus_id: str,
        query_vector: list[float],
        limit: int = 10,
        building_id: str | None = None,
        user_lat: float | None = None,
        user_lng: float | None = None,
        radius_m: float | None = None,
    ) -> list[dict]:
        """
        RAG retrieval — find the most contextually relevant spaces for a
        user query, with optional spatial filtering by distance from the
        user's GPS fix.

        Uses PostGIS + pgvector when ``pg_db`` is configured. That path
        is the spatial-query story for the project: a single SQL query
        can rank by cosine similarity AND filter by ``ST_DWithin``, so
        "similar spaces near me" is one indexed scan instead of two
        cross-store queries. Falls back to the legacy Neo4j vector
        index when PostGIS isn't wired up.
        """
        if self.pg_db is not None and self.pg_db.enabled:
            return self._search_similar_spaces_pg(
                campus_id, query_vector, limit, building_id,
                user_lat, user_lng, radius_m,
            )
        return self._search_similar_spaces_neo4j(
            campus_id, query_vector, limit, building_id,
        )

    def _search_similar_spaces_pg(
        self,
        campus_id: str,
        query_vector: list[float],
        limit: int,
        building_id: str | None,
        user_lat: float | None,
        user_lng: float | None,
        radius_m: float | None,
    ) -> list[dict]:
        # pgvector parses the literal "[v1,v2,...]" form natively; this
        # avoids needing the per-connection register_vector adapter when
        # using raw text() queries.
        q_literal = "[" + ",".join(f"{float(v):.7f}" for v in query_vector) + "]"

        spatial_filter = ""
        if user_lat is not None and user_lng is not None and radius_m is not None:
            spatial_filter = (
                "AND bs.geometry_global IS NOT NULL "
                "AND ST_DWithin("
                "bs.geometry_global::geography, "
                "ST_SetSRID(ST_MakePoint(:user_lng, :user_lat), 4326)::geography, "
                ":radius_m"
                ") "
            )

        sql = text(f"""
            SELECT
                bs.display_name                        AS name,
                bs.space_type                          AS type,
                f.display_name                         AS floor_name,
                b.name                                 AS building_name,
                1.0 - (bs.embedding <=> CAST(:q AS vector)) AS score,
                COALESCE(
                    (SELECT json_agg(json_build_object(
                        'name', nb.display_name,
                        'connection_type', sc.connection_type
                    ))
                     FROM space_connections sc
                     JOIN building_spaces nb ON nb.id = sc.to_space_id
                     WHERE sc.from_space_id = bs.id),
                    '[]'::json
                )                                      AS connected_to
            FROM building_spaces bs
            LEFT JOIN floors    f ON f.id = bs.floor_id
            LEFT JOIN buildings b ON b.id = bs.building_id
            WHERE bs.campus_id    = :campus_id
              AND bs.is_navigable = TRUE
              AND bs.embedding   IS NOT NULL
              AND (:building_id IS NULL OR bs.building_id = :building_id)
              {spatial_filter}
            ORDER BY bs.embedding <=> CAST(:q AS vector)
            LIMIT :limit
        """)

        params: dict = {
            "campus_id": campus_id,
            "q": q_literal,
            "limit": limit,
            "building_id": building_id,
        }
        if spatial_filter:
            params.update({
                "user_lat": user_lat,
                "user_lng": user_lng,
                "radius_m": radius_m,
            })

        with self.pg_db.SessionLocal() as session:
            rows = session.execute(sql, params).mappings().all()

        results: list[dict] = []
        for r in rows:
            conns = r["connected_to"] or []
            if isinstance(conns, str):
                try:
                    conns = json.loads(conns)
                except (ValueError, TypeError):
                    conns = []
            results.append({
                "name": r["name"],
                "type": r["type"],
                "floor_name": r["floor_name"],
                "building_name": r["building_name"],
                "connected_to": conns,
                "score": float(r["score"]) if r["score"] is not None else None,
            })
        return results

    def _search_similar_spaces_neo4j(
        self,
        campus_id: str,
        query_vector: list[float],
        limit: int,
        building_id: str | None,
    ) -> list[dict]:
        """Legacy Neo4j vector-index path. Kept as a fallback for deploys
        where pgvector isn't enabled yet."""
        cypher_query = """
        CALL db.index.vector.queryNodes('space_embedding_idx', $limit, $query_vector)
        YIELD node AS space, score
        WHERE space.campus_id = $campus_id AND space.is_navigable = true

        MATCH (building:Building)-[:HAS_FLOOR]->(floor:Floor)-[:HAS_SPACE]->(space)
        WHERE $building_id IS NULL OR building.id = $building_id

        OPTIONAL MATCH (space)-[r:CONNECTS_TO]-(neighbor:Space)

        RETURN
            space.display_name AS name,
            space.space_type AS type,
            floor.display_name AS floor_name,
            building.name AS building_name,
            collect(CASE WHEN neighbor IS NOT NULL THEN {
                name: neighbor.display_name,
                connection_type: type(r)
            } ELSE null END) AS connected_to,
            score
        ORDER BY score DESC
        """

        records = self.db.execute(
            cypher_query,
            {
                "campus_id": campus_id,
                "query_vector": query_vector,
                "limit": limit,
                "building_id": building_id,
            },
        )

        results = []
        for record in records:
            connections = [c for c in record["connected_to"] if c is not None]
            results.append({
                "name": record["name"],
                "type": record["type"],
                "floor_name": record["floor_name"],
                "building_name": record["building_name"],
                "connected_to": connections,
                "score": record["score"],
            })

        return results

    def get_anchor_space(
        self,
        campus_id: str,
        *,
        space_types: list[str],
        name_keywords: list[str] | None = None,
        tag_keywords: list[str] | None = None,
    ) -> dict | None:
        """
        Generic anchor selection for "distance from X" style queries.

        - space_types: acceptable Space.space_type values (ordered by preference)
        - name_keywords: optional keywords to prefer in display_name (ordered by preference)
        - tag_keywords: optional keywords to prefer in tags_text (ordered by preference)

        Returns: {id, name, cx, cy, type} or None
        """
        if not space_types:
            return None

        name_keywords = [k.lower() for k in (name_keywords or [])]
        tag_keywords = [k.lower() for k in (tag_keywords or [])]

        type_rank_cases = []
        for i, t in enumerate(space_types):
            type_rank_cases.append(f"WHEN s.space_type = '{t}' THEN {i}")
        type_rank = "CASE " + " ".join(type_rank_cases) + f" ELSE {len(space_types)} END"

        name_rank = "0"
        if name_keywords:
            name_rank_cases = []
            for i, kw in enumerate(name_keywords):
                name_rank_cases.append(f"WHEN n CONTAINS '{kw}' THEN {i}")
            name_rank = "CASE " + " ".join(name_rank_cases) + f" ELSE {len(name_keywords)} END"

        tag_rank = "0"
        if tag_keywords:
            tag_rank_cases = []
            for i, kw in enumerate(tag_keywords):
                tag_rank_cases.append(f"WHEN tt CONTAINS '{kw}' THEN {i}")
            tag_rank = "CASE " + " ".join(tag_rank_cases) + f" ELSE {len(tag_keywords)} END"

        cypher = (
            """
            MATCH (s:Space {campus_id: $campus_id})
            WHERE s.is_navigable = true AND s.space_type IN $space_types
            WITH s,
                 toLower(coalesce(s.display_name,'')) AS n,
                 toLower(coalesce(s.tags_text,'')) AS tt
            WITH s,
                 """
            + type_rank
            + """ AS type_rank,
                 """
            + name_rank
            + """ AS name_rank,
                 """
            + tag_rank
            + """ AS tag_rank
            ORDER BY type_rank ASC, name_rank ASC, tag_rank ASC, s.display_name ASC
            LIMIT 1
            RETURN s.id AS id, s.display_name AS name,
                   s.centroid_x AS cx, s.centroid_y AS cy, s.space_type AS type
            """
        )

        result = self.db.execute(
            cypher,
            {"campus_id": campus_id, "space_types": space_types},
        )
        return dict(result[0]) if result else None

    def extreme_space_by_distance(
        self,
        campus_id: str,
        *,
        anchor_space_id: str,
        candidate_space_types: list[str],
        extreme: str = "max",  # "max" | "min"
        gds_projection: str = "navigation-graph",
    ) -> dict | None:
        """
        Generic "pick closest/farthest space from anchor" primitive.

        Returns:
        {
          "anchor_id", "anchor_name",
          "target_id", "target_name",
          "distance_cost", "method"
        }
        """
        if not anchor_space_id or not candidate_space_types:
            return None
        if extreme not in ("max", "min"):
            extreme = "max"
        order = "DESC" if extreme == "max" else "ASC"

        # Shortest path via GDS
        try:
            rows = self.db.execute(
                """
                MATCH (a:Space {id: $anchor_id})
                MATCH (t:Space {campus_id: $campus_id})
                WHERE t.is_navigable = true
                  AND t.space_type IN $candidate_types
                  AND t.id <> $anchor_id
                CALL {
                  WITH a, t
                  CALL gds.shortestPath.dijkstra.stream($projection, {
                    sourceNode: a,
                    targetNode: t,
                    relationshipWeightProperty: 'weight'
                  })
                  YIELD totalCost
                  RETURN totalCost
                }
                RETURN
                  a.id AS anchor_id, a.display_name AS anchor_name,
                  t.id AS target_id, t.display_name AS target_name,
                  totalCost AS cost
                ORDER BY cost """
                + order
                + """
                LIMIT 1
                """,
                {
                    "campus_id": campus_id,
                    "anchor_id": anchor_space_id,
                    "candidate_types": candidate_space_types,
                    "projection": gds_projection,
                },
            )
            if rows:
                best = rows[0]
                return {
                    "anchor_id": best["anchor_id"],
                    "anchor_name": best["anchor_name"],
                    "target_id": best["target_id"],
                    "target_name": best["target_name"],
                    "distance_cost": float(best["cost"]) if best["cost"] is not None else None,
                    "method": "graph",
                }
        except Exception:
            pass

        # Euclidean Distance fallback
        try:
            anchor_rows = self.db.execute(
                """
                MATCH (a:Space {id: $anchor_id})
                RETURN a.display_name AS name, a.centroid_x AS cx, a.centroid_y AS cy
                """,
                {"anchor_id": anchor_space_id},
            )
            if not anchor_rows:
                return None
            anchor_name = anchor_rows[0].get("name")
            ex, ey = anchor_rows[0].get("cx"), anchor_rows[0].get("cy")
            if ex is None or ey is None:
                return None
            rows = self.db.execute(
                """
                MATCH (t:Space {campus_id: $campus_id})
                WHERE t.is_navigable = true
                  AND t.space_type IN $candidate_types
                  AND t.id <> $anchor_id
                  AND t.centroid_x IS NOT NULL AND t.centroid_y IS NOT NULL
                RETURN t.id AS target_id, t.display_name AS target_name, t.centroid_x AS cx, t.centroid_y AS cy
                """,
                {"campus_id": campus_id, "candidate_types": candidate_space_types, "anchor_id": anchor_space_id},
            )
            best = None
            best_d = None
            for r in rows:
                dx = float(r["cx"]) - float(ex)
                dy = float(r["cy"]) - float(ey)
                d = math.sqrt(dx * dx + dy * dy)
                if best_d is None:
                    best_d = d
                    best = r
                elif extreme == "max" and d > best_d:
                    best_d = d
                    best = r
                elif extreme == "min" and d < best_d:
                    best_d = d
                    best = r
            if best:
                return {
                    "anchor_id": anchor_space_id,
                    "anchor_name": anchor_name,
                    "target_id": best["target_id"],
                    "target_name": best["target_name"],
                    "distance_cost": float(best_d) if best_d is not None else None,
                    "method": "euclidean",
                }
        except Exception:
            return None

        return None

    def search_spaces_on_floor(
        self,
        campus_id: str,
        floor_index: int,
        limit: int = 20,
        building_id: str | None = None,
    ) -> list[dict]:
        """Return all navigable spaces on a specific floor, with their building/floor context."""
        cypher_query = """
        MATCH (building:Building)-[:HAS_FLOOR]->(floor:Floor)-[:HAS_SPACE]->(space:Space)
        WHERE space.campus_id = $campus_id
          AND floor.floor_index = $floor_index
          AND space.is_navigable = true
          AND ($building_id IS NULL OR building.id = $building_id)
        OPTIONAL MATCH (space)-[r:CONNECTS_TO]-(neighbor:Space)
        RETURN
            space.display_name AS name,
            space.space_type AS type,
            floor.display_name AS floor_name,
            floor.floor_index AS floor_index,
            building.name AS building_name,
            collect(CASE WHEN neighbor IS NOT NULL THEN {
                name: neighbor.display_name,
                connection_type: type(r)
            } ELSE null END) AS connected_to
        ORDER BY space.display_name ASC
        LIMIT $limit
        """
        records = self.db.execute(
            cypher_query,
            {
                "campus_id": campus_id,
                "floor_index": floor_index,
                "limit": limit,
                "building_id": building_id,
            },
        )
        results = []
        for record in records:
            connections = [c for c in record["connected_to"] if c is not None]
            results.append({
                "name": record["name"],
                "type": record["type"],
                "floor_name": record["floor_name"],
                "floor_index": record["floor_index"],
                "building_name": record["building_name"],
                "connected_to": connections,
            })
        return results

    def locate_user(
        self,
        campus_id: str,
        lat: float,
        lon: float,
        building_radius_m: float = 1500.0,
    ) -> dict | None:
        
        rows = self.db.execute(
            """
            MATCH (b:Building)
            WHERE b.campus_id = $campus_id OR b.id = $campus_id
            OPTIONAL MATCH (b)-[:HAS_FLOOR]->(f:Floor)-[:HAS_SPACE]->(s:Space)
            RETURN
              b.id AS building_id,
              b.name AS building_name,
              b.origin_lat AS b_lat,
              b.origin_lng AS b_lng,
              b.origin_bearing AS b_bearing,
              b.scale_factor AS b_scale,
              f.id AS floor_id,
              f.display_name AS floor_name,
              f.origin_lat AS f_lat,
              f.origin_lng AS f_lng,
              f.origin_bearing AS f_bearing,
              f.scale_factor AS f_scale,
              s.display_name AS name,
              s.centroid_x AS cx,
              s.centroid_y AS cy,
              s.polygon AS polygon
            """,
            {"campus_id": campus_id},
        )
        if not rows:
            return None

        best_inside: dict | None = None
        nearest: dict | None = None
        nearest_d: float = float("inf")

        for r in rows:
            cx, cy = r["cx"], r["cy"]
            if cx is None or cy is None:
                continue

            # acceptable - we just need *some* origin to project from.
            origin_lat = r["f_lat"] if r["f_lat"] is not None else r["b_lat"]
            origin_lng = r["f_lng"] if r["f_lng"] is not None else r["b_lng"]
            bearing = (r["f_bearing"] if r["f_bearing"] is not None else r["b_bearing"]) or 0.0
            scale = (r["f_scale"] if r["f_scale"] is not None else r["b_scale"]) or 1.0
            if origin_lat is None or origin_lng is None:
                continue

            b_distance = _haversine_m(lat, lon, origin_lat, origin_lng)
            if b_distance > building_radius_m:
                continue

            s_lat, s_lng = _project_local_to_global(
                cx, cy, origin_lat, origin_lng, bearing, scale,
            )
            d = _haversine_m(lat, lon, s_lat, s_lng)
            if d < nearest_d:
                nearest_d = d
                nearest = {
                    "inside": False,
                    "distance_m": d,
                    "name": r["name"],
                    "floor_name": r["floor_name"],
                    "building_name": r["building_name"],
                    "building_id": r.get("building_id"),
                }

            poly_raw = r["polygon"]
            if not poly_raw:
                continue
            poly_local = poly_raw
            if isinstance(poly_raw, str):
                try:
                    poly_local = json.loads(poly_raw)
                except (ValueError, TypeError):
                    continue
            poly_global = []
            for pt in poly_local:
                try:
                    px, py = float(pt[0]), float(pt[1])
                except (TypeError, ValueError, IndexError):
                    continue
                p_lat, p_lng = _project_local_to_global(
                    px, py, origin_lat, origin_lng, bearing, scale,
                )
                poly_global.append([p_lat, p_lng])
            if _point_in_polygon((lat, lon), poly_global):
                if best_inside is None or d < best_inside["distance_m"]:
                    best_inside = {
                        "inside": True,
                        "distance_m": d,
                        "name": r["name"],
                        "floor_name": r["floor_name"],
                        "building_name": r["building_name"],
                        "building_id": r.get("building_id"),
                    }

        return best_inside or nearest

    def get_main_entrance(self, campus_id: str) -> dict | None:
        return self.get_anchor_space(
            campus_id,
            space_types=["ENTRANCE", "LOBBY", "ENTRANCE_SECONDARY"],
            name_keywords=["main", "entrance", "front"],
            tag_keywords=["main_entrance", "main entrance", "entrance"],
        )

    # To change to be more generic, not just offices
    def farthest_office_from_main_entrance(self, campus_id: str, gds_projection: str = "navigation-graph") -> dict | None:
        entrance = self.get_main_entrance(campus_id)
        if not entrance:
            return None
        r = self.extreme_space_by_distance(
            campus_id,
            anchor_space_id=entrance["id"],
            candidate_space_types=["ROOM_OFFICE"],
            extreme="max",
            gds_projection=gds_projection,
        )
        if not r:
            return None
        return {
            "office_id": r["target_id"],
            "office_name": r["target_name"],
            "entrance_id": r["anchor_id"],
            "entrance_name": r["anchor_name"],
            "distance_cost": r["distance_cost"],
            "method": r["method"],
        }
