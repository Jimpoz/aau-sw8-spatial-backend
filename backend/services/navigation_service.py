import json

from db import Database
from models.navigation import Route, RouteStep, FloorChange, BuildingChange
from models.enums import SpaceType
from repositories.navigation_repo import NavigationRepository
from services.geometry_service import (
    closest_point_on_polygon,
    find_shared_edge_midpoint,
    local_to_global_coordinates,
    parse_polygon,
)


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
        self.db = db
        self.repo = NavigationRepository(db)

    def _georef_by_id(self, space_ids: list[str]) -> dict[str, dict]:
        """Bulk-fetch the (floor-override → building-inherit) georef for
        every Space id. Cached per request so the polyline builder and
        the centroid enricher share one Neo4j round-trip."""
        if not space_ids:
            return {}
        rows = self.db.execute(
            """
            MATCH (s:Space) WHERE s.id IN $ids
            OPTIONAL MATCH (b1:Building)-[:HAS_FLOOR]->(f:Floor)-[:HAS_SPACE]->(s)
            OPTIONAL MATCH (b2:Building {id: s.building_id})
            WITH s, f, coalesce(b1, b2) AS b
            RETURN s.id AS id,
                   f.origin_lat AS f_lat, f.origin_lng AS f_lng,
                   f.origin_bearing AS f_bearing, f.scale_factor AS f_scale,
                   b.origin_lat AS b_lat, b.origin_lng AS b_lng,
                   b.origin_bearing AS b_bearing, b.scale_factor AS b_scale
            """,
            {"ids": list(space_ids)},
        )
        out: dict[str, dict] = {}
        for r in rows:
            origin_lat = r["f_lat"] if r["f_lat"] is not None else r["b_lat"]
            origin_lng = r["f_lng"] if r["f_lng"] is not None else r["b_lng"]
            bearing = (r["f_bearing"] if r["f_bearing"] is not None else r["b_bearing"]) or 0.0
            scale = (r["f_scale"] if r["f_scale"] is not None else r["b_scale"]) or 1.0
            out[r["id"]] = {
                "origin_lat": origin_lat,
                "origin_lng": origin_lng,
                "bearing": bearing,
                "scale": scale,
            }
        return out

    _ROOM_PULL_FACTOR: float = 0.35

    def _build_polyline(
        self,
        path_nodes: list[dict],
        georefs: dict[str, dict],
    ) -> list[list[float]]:
        """Build the rendered route line."""

        if len(path_nodes) < 2:
            return [
                [n["centroid_lat"], n["centroid_lng"]]
                for n in path_nodes
                if n.get("centroid_lat") is not None
                and n.get("centroid_lng") is not None
            ]

        n = len(path_nodes)
        transitions: list[list[float] | None] = [
            self._shared_edge_waypoint(path_nodes[i], path_nodes[i + 1], georefs)
            for i in range(n - 1)
        ]

        def _global_centroid(node: dict, i: int) -> list[float] | None:
            """Resolve a node's centroid in world coords, projecting
            from local coords via a neighbor's georef if the globals
            are missing (e.g. door Spaces with no HAS_SPACE link AND
            no building_id property)."""
            lat = node.get("centroid_lat")
            lng = node.get("centroid_lng")
            if (lat is None or lng is None) and node.get("centroid_x") is not None and node.get("centroid_y") is not None:
                neighbor_g = None
                for nbr_idx in (i - 1, i + 1):
                    if 0 <= nbr_idx < n:
                        g = georefs.get(path_nodes[nbr_idx]["id"])
                        if g and g["origin_lat"] is not None and g["origin_lng"] is not None:
                            neighbor_g = g
                            break
                if neighbor_g is not None:
                    lat, lng = local_to_global_coordinates(
                        node["centroid_x"], node["centroid_y"],
                        neighbor_g["origin_lat"], neighbor_g["origin_lng"],
                        neighbor_g["bearing"], neighbor_g["scale"],
                    )
                    node["centroid_lat"] = lat
                    node["centroid_lng"] = lng
            if lat is None or lng is None:
                return None
            return [float(lat), float(lng)]

        def _endpoint_exit_point(node: dict, target: dict) -> list[float] | None:
            """Snap the start or end of the polyline from a room's centroid
            to the point on its polygon boundary closest to the adjacent
            connector. 

            Returns ``None`` when the polygon, georef, or target's local
            coordinates aren't available — caller falls back to the
            centroid in that case."""
            polygon = parse_polygon(node.get("polygon"))
            if polygon is None:
                return None
            tx = target.get("centroid_x")
            ty = target.get("centroid_y")
            if tx is None or ty is None:
                return None
            g = georefs.get(node["id"])
            if g is None or g.get("origin_lat") is None or g.get("origin_lng") is None:
                return None
            sx, sy = closest_point_on_polygon(polygon, float(tx), float(ty))
            lat, lng = local_to_global_coordinates(
                sx, sy,
                g["origin_lat"], g["origin_lng"],
                g["bearing"], g["scale"],
            )
            return [float(lat), float(lng)]

        centroids: list[list[float] | None] = [
            _global_centroid(node, i) for i, node in enumerate(path_nodes)
        ]

        def _is_anchor(j: int) -> bool:
            """Endpoint or connector — a node whose centroid we keep
            as-is. These are the points the room-pull computation
            triangulates against."""
            if j == 0 or j == n - 1:
                return True
            return self._is_connector(path_nodes[j])

        def _pulled_in_waypoint(i: int) -> list[float] | None:
            room_c = centroids[i]
            if room_c is None:
                return None
            prev_pt: list[float] | None = None
            for j in range(i - 1, -1, -1):
                if _is_anchor(j) and centroids[j] is not None:
                    prev_pt = centroids[j]
                    break
            next_pt: list[float] | None = None
            for j in range(i + 1, n):
                if _is_anchor(j) and centroids[j] is not None:
                    next_pt = centroids[j]
                    break
            if prev_pt is None or next_pt is None:
                return room_c
            mid_lat = (prev_pt[0] + next_pt[0]) / 2.0
            mid_lng = (prev_pt[1] + next_pt[1]) / 2.0
            alpha = self._ROOM_PULL_FACTOR
            return [
                mid_lat + alpha * (room_c[0] - mid_lat),
                mid_lng + alpha * (room_c[1] - mid_lng),
            ]

        snapped_start: list[float] | None = None
        snapped_end: list[float] | None = None
        if not self._is_connector(path_nodes[0]) and n >= 2:
            snapped_start = _endpoint_exit_point(path_nodes[0], path_nodes[1])
        if not self._is_connector(path_nodes[n - 1]) and n >= 2:
            snapped_end = _endpoint_exit_point(path_nodes[n - 1], path_nodes[n - 2])

        polyline: list[list[float]] = []
        for i in range(n):
            if _is_anchor(i):
                point: list[float] | None
                if i == 0 and snapped_start is not None:
                    point = snapped_start
                elif i == n - 1 and snapped_end is not None:
                    point = snapped_end
                else:
                    point = centroids[i]
                if point is not None:
                    polyline.append(point)
            else:
                pulled = _pulled_in_waypoint(i)
                if pulled is not None:
                    polyline.append(pulled)
            if i < n - 1 and transitions[i] is not None:
                polyline.append(transitions[i])
        return polyline

    _CONNECTOR_SPACE_TYPES: frozenset[str] = frozenset({
        "PASSAGE",
        "ENTRANCE", "ENTRANCE_SECONDARY", "EXIT_EMERGENCY",
        "STAIRCASE", "ELEVATOR", "ESCALATOR", "RAMP",
    })

    @classmethod
    def _is_connector(cls, node: dict) -> bool:
        st = (node.get("space_type") or "").upper()
        if st.startswith("DOOR_"):
            return True
        return st in cls._CONNECTOR_SPACE_TYPES

    def _shared_edge_waypoint(
        self,
        a: dict,
        b: dict,
        georefs: dict[str, dict],
    ) -> list[float] | None:
        """Return the world-coord midpoint of the wall shared by spaces
        a and b, or None if either side has no polygon / no usable
        georef. This is where the route line should pass through for the most accurate rendering (e.g. the door location instead of the room centroid)."""
        if (a.get("building_id") != b.get("building_id")
                or a.get("floor_index") != b.get("floor_index")):
            return None

        ga = georefs.get(a["id"]) or georefs.get(b["id"])
        if not ga or ga["origin_lat"] is None or ga["origin_lng"] is None:
            return None

        poly_a = self._parse_polygon(a.get("polygon"))
        poly_b = self._parse_polygon(b.get("polygon"))
        if not poly_a or not poly_b:
            return None
        try:
            mid = find_shared_edge_midpoint(poly_a, poly_b, eps=1.0)
        except Exception:
            return None
        if mid is None:
            return None
        mx, my = mid
        lat, lng = local_to_global_coordinates(
            mx, my, ga["origin_lat"], ga["origin_lng"], ga["bearing"], ga["scale"],
        )
        return [float(lat), float(lng)]

    @staticmethod
    def _parse_polygon(raw) -> list | None:
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return None
        if not isinstance(raw, list) or len(raw) < 3:
            return None
        return raw

    def _enrich_global_coords(
        self, path_nodes: list[dict], georefs: dict[str, dict]
    ) -> None:
        """Fill missing centroid_lat/lng on every step using the
        prefetched georef cache."""
        for n in path_nodes:
            if n.get("centroid_lat") is not None and n.get("centroid_lng") is not None:
                continue
            cx, cy = n.get("centroid_x"), n.get("centroid_y")
            if cx is None or cy is None:
                continue
            g = georefs.get(n["id"])
            if not g or g["origin_lat"] is None or g["origin_lng"] is None:
                continue
            lat, lng = local_to_global_coordinates(
                cx, cy, g["origin_lat"], g["origin_lng"], g["bearing"], g["scale"],
            )
            n["centroid_lat"] = lat
            n["centroid_lng"] = lng

    def get_route(
        self,
        from_space_id: str,
        to_space_id: str,
        accessible_only: bool = False,
        avoid_stairs: bool = False,
        elevators_only: bool = False,
    ) -> Route:
        raw = self.repo.find_path(
            from_space_id,
            to_space_id,
            accessible_only=accessible_only,
            avoid_stairs=avoid_stairs,
            elevators_only=elevators_only,
        )

        path_nodes: list[dict] = raw["path_nodes"]
        total_cost: float = raw["total_cost"] or 0.0

        georefs = self._georef_by_id([n["id"] for n in path_nodes])

        self._enrich_global_coords(path_nodes, georefs)

        polyline = self._build_polyline(path_nodes, georefs)

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
            polyline=polyline,
        )
