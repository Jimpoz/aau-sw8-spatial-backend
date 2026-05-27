from db import Database

_PROJECTION_NAME = "navigation-graph"

_WALKING_SPEED_MS = 1.4

_CONNECTION_TYPES = [
    "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED", "DOOR_EMERGENCY",
    "PASSAGE", "OPEN",
    "ELEVATOR", "STAIRCASE", "ESCALATOR", "RAMP",
]


class GdsService:
    def __init__(self, db: Database):
        self.db = db

    def projection_exists(self, name: str = _PROJECTION_NAME) -> bool:
        try:
            result = self.db.execute(
                "CALL gds.graph.exists($name) YIELD exists RETURN exists",
                {"name": name},
            )
            return result[0]["exists"] if result else False
        except Exception:
            return False

    def drop_projection(self, name: str = _PROJECTION_NAME) -> None:
        try:
            self.db.execute_write(
                "CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName",
                {"name": name},
            )
        except Exception:
            pass

    def create_projection(self, name: str = _PROJECTION_NAME) -> bool:
        """Project navigable Space nodes and CONNECTS_TO relationships into GDS."""
        list_literal = "[" + ", ".join(f"'{t}'" for t in _CONNECTION_TYPES) + "]"

        rel_query = (
            "MATCH (s:Space)-[:CONNECTS_TO]->(t:Space) "
            "WHERE s.is_navigable = true AND t.is_navigable = true "
            "WITH s, t, "
            "     CASE "
            "       WHEN s.floor_index = t.floor_index "
            "        AND s.building_id = t.building_id "
            "        AND s.centroid_x IS NOT NULL AND s.centroid_y IS NOT NULL "
            "        AND t.centroid_x IS NOT NULL AND t.centroid_y IS NOT NULL "
            "       THEN sqrt( "
            "              (s.centroid_x - t.centroid_x) * (s.centroid_x - t.centroid_x) "
            "            + (s.centroid_y - t.centroid_y) * (s.centroid_y - t.centroid_y) "
            "            ) "
            "       ELSE 1.0 "
            "     END AS dist_m, "
            f"    CASE WHEN t.space_type IN {list_literal} "
            "          THEN coalesce(t.traversal_cost, 0.0) "
            "          ELSE 0.0 "
            "     END AS penalty_s "
            f"RETURN id(s) AS source, id(t) AS target, "
            f"       (dist_m / {_WALKING_SPEED_MS}) + penalty_s AS weight"
        )
        try:
            rows = self.db.execute_write(
                """
                CALL gds.graph.project.cypher(
                    $name,
                    'MATCH (s:Space) WHERE s.is_navigable = true RETURN id(s) AS id',
                    $rel_query
                )
                YIELD graphName, nodeCount, relationshipCount
                RETURN graphName, nodeCount, relationshipCount
                """,
                {"name": name, "rel_query": rel_query},
            )
            if rows:
                r = rows[0]
                print(
                    f"[gds] projection '{name}' created: "
                    f"{r.get('nodeCount')} nodes, "
                    f"{r.get('relationshipCount')} relationships, "
                    f"weighted by walking-time seconds",
                    flush=True,
                )
            else:
                print(
                    f"[gds] projection '{name}' create call returned no rows",
                    flush=True,
                )
            return True
        except Exception as exc:
            print(
                f"[gds] projection '{name}' creation FAILED: {exc!r}\n"
                f"[gds] rel_query was:\n{rel_query}",
                flush=True,
            )
            return False

    def refresh_projection(self, name: str = _PROJECTION_NAME) -> bool:
        self.drop_projection(name)
        return self.create_projection(name)
