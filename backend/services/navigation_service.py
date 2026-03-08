from db import Database
from models.navigation import Route, RouteStep, FloorChange, BuildingChange
from models.enums import ConnectionType, SpaceType
from repositories.navigation_repo import NavigationRepository

_STAIR_TYPES = {ConnectionType.STAIRCASE_UP, ConnectionType.STAIRCASE_DOWN}
_ELEVATOR_TYPES = {ConnectionType.ELEVATOR_UP, ConnectionType.ELEVATOR_DOWN}
_ESCALATOR_TYPES = {ConnectionType.ESCALATOR_UP, ConnectionType.ESCALATOR_DOWN}
_VERTICAL_TYPES = _STAIR_TYPES | _ELEVATOR_TYPES | _ESCALATOR_TYPES


def _instruction(node: dict, connection_type: str | None) -> str:
    name = node.get("display_name", "")
    space_type = node.get("space_type", "")
    ct = connection_type or ""

    if ct == "STAIRCASE_UP":
        return f"Take stairs up to {name}"
    if ct == "STAIRCASE_DOWN":
        return f"Take stairs down to {name}"
    if ct == "ELEVATOR_UP":
        return f"Take elevator up to {name}"
    if ct == "ELEVATOR_DOWN":
        return f"Take elevator down to {name}"
    if ct == "ESCALATOR_UP":
        return f"Take escalator up to {name}"
    if ct == "ESCALATOR_DOWN":
        return f"Take escalator down to {name}"
    if ct == "BRIDGE":
        return f"Cross bridge to {name}"
    if ct == "TUNNEL":
        return f"Go through tunnel to {name}"
    if space_type in ("ENTRANCE", "ENTRANCE_SECONDARY"):
        return f"Enter through {name}"
    if space_type == "EXIT_EMERGENCY":
        return f"Exit via {name}"
    if space_type in ("CORRIDOR", "CORRIDOR_SEGMENT"):
        return f"Walk through {name}"
    if space_type == "LOBBY":
        return f"Cross {name}"
    return f"Go to {name}"


class NavigationService:
    def __init__(self, db: Database):
        self.repo = NavigationRepository(db)

    def get_route(
        self,
        from_space_id: str,
        to_space_id: str,
        accessible_only: bool = False,
    ) -> Route:
        raw = self.repo.find_path(from_space_id, to_space_id, accessible_only)

        path_nodes: list[dict] = raw["path_nodes"]
        path_rels: list[dict] = raw["path_rels"]
        total_cost: float = raw["total_cost"] or 0.0

        steps: list[RouteStep] = []
        floor_changes: list[FloorChange] = []
        building_changes: list[BuildingChange] = []

        for i, node in enumerate(path_nodes):
            rel = path_rels[i - 1] if i > 0 else None
            conn_type_str = rel["connection_type"] if rel else None

            try:
                conn_type = ConnectionType(conn_type_str) if conn_type_str else None
            except ValueError:
                conn_type = None

            try:
                space_type = SpaceType(node.get("space_type", "UNKNOWN"))
            except ValueError:
                space_type = SpaceType.UNKNOWN

            step = RouteStep(
                space_id=node["id"],
                display_name=node.get("display_name", ""),
                space_type=space_type,
                floor_index=node.get("floor_index"),
                building_id=node.get("building_id"),
                centroid_x=node.get("centroid_x"),
                centroid_y=node.get("centroid_y"),
                connection_type=conn_type,
                instruction=_instruction(node, conn_type_str) if i > 0 else None,
                cost=rel["weight"] if rel else None,
            )
            steps.append(step)

            # Detect floor change
            if i > 0:
                prev = path_nodes[i - 1]
                if (
                    prev.get("floor_index") is not None
                    and node.get("floor_index") is not None
                    and prev["floor_index"] != node["floor_index"]
                ):
                    floor_changes.append(
                        FloorChange(
                            from_floor=prev["floor_index"],
                            to_floor=node["floor_index"],
                            at_space_id=node["id"],
                            connection_type=conn_type or ConnectionType.WALKWAY,
                        )
                    )

                # Detect building change
                if (
                    prev.get("building_id") != node.get("building_id")
                    and not (prev.get("building_id") is None and node.get("building_id") is None)
                ):
                    building_changes.append(
                        BuildingChange(
                            from_building_id=prev.get("building_id"),
                            to_building_id=node.get("building_id"),
                            at_space_id=node["id"],
                        )
                    )

        total_dist = sum(
            r.get("distance_m") or 0.0 for r in path_rels if r.get("distance_m") is not None
        ) or None

        return Route(
            from_space_id=from_space_id,
            to_space_id=to_space_id,
            total_cost=total_cost,
            total_distance_m=total_dist,
            steps=steps,
            floor_changes=floor_changes,
            building_changes=building_changes,
        )
