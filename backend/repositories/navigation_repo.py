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
        centroid_y: n.centroid_y,
        centroid_lat: n.centroid_lat,
        centroid_lng: n.centroid_lng,
        traversal_cost: n.traversal_cost
    }] AS path_nodes,
    totalCost
"""

_NATIVE_QUERY = """
MATCH (start:Space {id: $from_id}), (end:Space {id: $to_id})
MATCH path = shortestPath(
    (start)-[:CONNECTS_TO*..100]->(end)
)
WHERE ALL(n IN nodes(path) WHERE n.is_navigable = true OR n.id IN [$from_id, $to_id])
    AND ($accessible_only = false OR ALL(n IN nodes(path) WHERE n.is_accessible = true))
    AND ALL(
        n IN nodes(path)
        WHERE n.id IN [$from_id, $to_id] OR NOT n.space_type IN $excluded_types
    )
RETURN
    [n IN nodes(path) | {
        id: n.id,
        display_name: n.display_name,
        space_type: n.space_type,
        floor_index: n.floor_index,
        building_id: n.building_id,
        campus_id: n.campus_id,
        centroid_x: n.centroid_x,
        centroid_y: n.centroid_y,
        centroid_lat: n.centroid_lat,
        centroid_lng: n.centroid_lng,
        traversal_cost: n.traversal_cost
    }] AS path_nodes,
    reduce(cost = 0.0, n IN nodes(path) | cost + coalesce(n.traversal_cost, 0.0)) AS totalCost
LIMIT 1
"""


def _excluded_types(avoid_stairs: bool, elevators_only: bool) -> list[str]:
    """Vertical-transport space types to keep out of the path."""
    excluded: set[str] = set()
    if avoid_stairs:
        excluded.update({"STAIRCASE", "ESCALATOR"})
    if elevators_only:
        excluded.update({"STAIRCASE", "ESCALATOR", "RAMP"})
    return sorted(excluded)


class NavigationRepository:
    def __init__(self, db: Database):
        self.db = db

    def find_path(
        self,
        from_id: str,
        to_id: str,
        accessible_only: bool = False,
        avoid_stairs: bool = False,
        elevators_only: bool = False,
        gds_projection: str = "navigation-graph",
    ) -> dict:
        """Return {path_nodes, total_cost} or raise NavigationError."""
        # Verify both spaces exist
        for space_id in (from_id, to_id):
            check = self.db.execute(
                "MATCH (s:Space {id: $id}) RETURN s.id AS id",
                {"id": space_id},
            )
            if not check:
                raise SpaceNotFound(space_id)

        excluded = _excluded_types(avoid_stairs, elevators_only)

        if not accessible_only and not excluded:
            try:
                result = self.db.execute(
                    _GDS_QUERY,
                    {"from_id": from_id, "to_id": to_id, "projection": gds_projection},
                )
                if result and result[0]["path_nodes"]:
                    row = result[0]
                    return {
                        "path_nodes": row["path_nodes"],
                        "total_cost": row["totalCost"],
                    }
            except Exception:
                pass

        result = self.db.execute(
            _NATIVE_QUERY,
            {
                "from_id": from_id,
                "to_id": to_id,
                "accessible_only": accessible_only,
                "excluded_types": excluded,
            },
        )
        if not result or not result[0]["path_nodes"]:
            filters: list[str] = []
            if accessible_only:
                filters.append("accessible")
            if elevators_only:
                filters.append("elevators-only")
            elif avoid_stairs:
                filters.append("no-stairs")
            label = ", ".join(filters)
            raise NavigationError(
                f"No{' ' + label if label else ''} path found "
                f"from '{from_id}' to '{to_id}'"
            )
        row = result[0]
        return {
            "path_nodes": row["path_nodes"],
            "total_cost": row["totalCost"],
        }
