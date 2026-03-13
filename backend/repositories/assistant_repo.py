from db import Database
import math

class AssistantRepository:
    def __init__(self, db: Database):
        self.db = db

    def search_similar_spaces(
        self,
        campus_id: str,
        query_vector: list[float],
        limit: int = 10,
    ) -> list[dict]:
        """
        Performs a vector search to find the most contextually relevant spaces, 
        and then traverses the graph to find their physical location and connected neighbors.
        Also helping in understanding how they are connected to each other and the physical context of the building.
        """
        
        cypher_query = """
        // Semanti Search
        CALL db.index.vector.queryNodes('space_embedding_idx', $limit, $query_vector)
        YIELD node AS space, score
        WHERE space.campus_id = $campus_id AND space.is_navigable = true
        
        // Vertical Context
        MATCH (building:Building)-[:HAS_FLOOR]->(floor:Floor)-[:HAS_SPACE]->(space)
        
        // Horizontal Context
        // We use OPTIONAL MATCH so the query doesn't fail if a room has no connections yet
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
            {"campus_id": campus_id, "query_vector": query_vector, "limit": limit}
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
                "score": record["score"]
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