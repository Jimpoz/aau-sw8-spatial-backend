"""Helpers that bridge Neo4j-shaped space dicts to the PostGIS `sync_space`
payload. Used by every CRUD route that mutates a Space so Neo4j and Supabase
stay in lockstep."""

from repositories.campus_repo import CampusRepository
from services.geometry_service import (
    local_to_global_coordinates,
    polygon_local_to_global,
)


def _space_type_str(value) -> str | None:
    """Accept enum instances or bare strings."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def build_space_sync_payload(
    space: dict,
    campus_repo: CampusRepository,
) -> dict:
    """Turn a Space dict (as stored in Neo4j) into the payload that
    PostGISService.sync_space expects, resolving global lat/lng/polygon
    from the parent building's origin if available."""
    building_id = space.get("building_id")
    cx = space.get("centroid_x")
    cy = space.get("centroid_y")
    polygon = space.get("polygon")

    global_lat, global_lng, global_polygon = None, None, None
    if building_id and cx is not None and cy is not None:
        try:
            building = campus_repo.get_building(building_id)
        except Exception:
            building = None
        if building and building.get("origin_lat") is not None and building.get("origin_lng") is not None:
            bearing = building.get("origin_bearing") or 0.0
            global_lat, global_lng = local_to_global_coordinates(
                cx, cy, building["origin_lat"], building["origin_lng"], bearing,
            )
            if polygon:
                global_polygon = polygon_local_to_global(
                    polygon, building["origin_lat"], building["origin_lng"], bearing,
                )

    return {
        "id": space["id"],
        "organization_id": space.get("organization_id"),
        "campus_id": space.get("campus_id"),
        "building_id": building_id,
        "floor_id": space.get("floor_id"),
        "display_name": space.get("display_name"),
        "space_type": _space_type_str(space.get("space_type")),
        "floor_index": space.get("floor_index"),
        "centroid_x": cx,
        "centroid_y": cy,
        "centroid_lat": space.get("centroid_lat") or global_lat,
        "centroid_lng": space.get("centroid_lng") or global_lng,
        "width_m": space.get("width_m"),
        "length_m": space.get("length_m"),
        "area_m2": space.get("area_m2"),
        "polygon": polygon,
        "polygon_global": space.get("polygon_global") or global_polygon,
        "is_accessible": space.get("is_accessible", True),
        "is_navigable": space.get("is_navigable", True),
        "is_outdoor": space.get("is_outdoor", False),
        "capacity": space.get("capacity"),
        "tags": space.get("tags", []),
        "metadata": space.get("metadata", {}),
    }
