from db import Database
from core.exceptions import NavigationError, SpaceNotFound


_GDS_QUERY = """
MATCH (start:Space {id: $from_id}), (end:Space {id: $to_id})
CALL gds.shortestPath.dijkstra.stream($projection, {
    sourceNode: start,
    targetNode: end,
    relationshipWeightProperty: 'weight'
})
YIELD path, totalCost
RETURN
    [n IN nodes(path) | {
        id: n.id,
        display_name: n.display_name,
        space_type: n.space_type,
        floor_index: n.floor_index,
        building_id: n.building_id,
        campus_id: n.campus_id,
        centroid_x: n.centroid_x,
        centroid_y: n.centroid_y
    }] AS path_nodes,
    [r IN relationships(path) | {
        connection_type: r.connection_type,
        weight: r.weight,
        distance_m: r.distance_m,
        is_accessible: r.is_accessible
    }] AS path_rels,
    totalCost
"""

_NATIVE_QUERY = """
MATCH (start:Space {id: $from_id}), (end:Space {id: $to_id})
MATCH path = shortestPath(
    (start)-[c:CONNECTS_TO*..100 WHERE ($accessible_only = false OR c.is_accessible = true)]->(end)
)
WHERE ALL(n IN nodes(path) WHERE n.is_navigable = true OR n.id IN [$from_id, $to_id])
RETURN
    [n IN nodes(path) | {
        id: n.id,
        display_name: n.display_name,
        space_type: n.space_type,
        floor_index: n.floor_index,
        building_id: n.building_id,
        campus_id: n.campus_id,
        centroid_x: n.centroid_x,
        centroid_y: n.centroid_y
    }] AS path_nodes,
    [r IN relationships(path) | {
        connection_type: r.connection_type,
        weight: r.weight,
        distance_m: r.distance_m,
        is_accessible: r.is_accessible
    }] AS path_rels,
    reduce(cost = 0.0, r IN relationships(path) | cost + coalesce(r.weight, 0.0)) AS totalCost
LIMIT 1
"""


class NavigationRepository:
    def __init__(self, db: Database):
        self.db = db

    def find_path(
        self,
        from_id: str,
        to_id: str,
        accessible_only: bool = False,
        gds_projection: str = "navigation-graph",
    ) -> dict:
        """Return {path_nodes, path_rels, total_cost} or raise NavigationError."""
        # Verify both spaces exist
        for space_id in (from_id, to_id):
            check = self.db.execute(
                "MATCH (s:Space {id: $id}) RETURN s.id AS id",
                {"id": space_id},
            )
            if not check:
                raise SpaceNotFound(space_id)

        # Try GDS Dijkstra first (weighted, ignores accessible_only filter at graph level)
        if not accessible_only:
            try:
                result = self.db.execute(
                    _GDS_QUERY,
                    {"from_id": from_id, "to_id": to_id, "projection": gds_projection},
                )
                if result and result[0]["path_nodes"]:
                    row = result[0]
                    return {
                        "path_nodes": row["path_nodes"],
                        "path_rels": row["path_rels"],
                        "total_cost": row["totalCost"],
                    }
            except Exception:
                pass  # GDS not available or projection missing — fall through

        # Fallback: native shortestPath (hop-count shortest, with accessibility filter)
        result = self.db.execute(
            _NATIVE_QUERY,
            {"from_id": from_id, "to_id": to_id, "accessible_only": accessible_only},
        )
        if not result or not result[0]["path_nodes"]:
            raise NavigationError(
                f"No {'accessible ' if accessible_only else ''}path found "
                f"from '{from_id}' to '{to_id}'"
            )
        row = result[0]
        return {
            "path_nodes": row["path_nodes"],
            "path_rels": row["path_rels"],
            "total_cost": row["totalCost"],
        }
