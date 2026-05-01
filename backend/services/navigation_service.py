from db import Database
from models.navigation import Route, RouteStep, FloorChange, BuildingChange
from models.enums import SpaceType
from repositories.navigation_repo import NavigationRepository


def _instruction(node: dict) -> str:
    name = node.get("display_name", "")
    space_type = node.get("space_type", "")

    if space_type.startswith("DOOR_"):
        return f"Go through door to {name}"
    if space_type == "PASSAGE":
        return f"Continue to {name}"
    if space_type == "STAIRCASE":
        return f"Take stairs to {name}"
    if space_type == "ELEVATOR":
        return f"Take elevator to {name}"
    if space_type == "ESCALATOR":
        return f"Take escalator to {name}"
    if space_type == "RAMP":
        return f"Take ramp to {name}"
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
        total_cost: float = raw["total_cost"] or 0.0

        steps: list[RouteStep] = []
        floor_changes: list[FloorChange] = []
        building_changes: list[BuildingChange] = []

        for i, node in enumerate(path_nodes):
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
                centroid_lat=node.get("centroid_lat"),
                centroid_lng=node.get("centroid_lng"),
                instruction=_instruction(node) if i > 0 else None,
                cost=node.get("traversal_cost"),
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

        return Route(
            from_space_id=from_space_id,
            to_space_id=to_space_id,
            total_cost=total_cost,
            steps=steps,
            floor_changes=floor_changes,
            building_changes=building_changes,
        )
