from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.auth_principal import Principal, require_org_match, require_role
from core.exceptions import BuildingNotFound, FloorNotFound
from models.campus import Floor, FloorCreate, FloorUpdate
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from services.audit_service import audit_action
from services.geometry_service import (
    local_to_global_coordinates,
    polygon_local_to_global,
)
from services.postgis_service import PostGISService

router = APIRouter(prefix="/floors", tags=["floors"])


def _resolve_floor_origin(floor: dict, building: dict) -> dict:
    """Fill a floor's georeferencing fields from the building when the floor
    has no override of its own, so callers always see concrete values."""
    resolved = dict(floor)
    for field, default in (
        ("origin_lat", building.get("origin_lat")),
        ("origin_lng", building.get("origin_lng")),
        ("origin_bearing", building.get("origin_bearing") or 0.0),
        ("scale_factor", building.get("scale_factor") or 1.0),
    ):
        if resolved.get(field) is None:
            resolved[field] = default
    return resolved


@router.post("", response_model=Floor, status_code=201)
def create_floor(
    data: FloorCreate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    try:
        parent = CampusRepository(db).get_building(data.building_id)
    except BuildingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = parent.get("organization_id") if isinstance(parent, dict) else None
    require_org_match(principal, org_id)
    with audit_action("create_floor", principal, organization_id=org_id) as detail:
        detail["building_id"] = data.building_id
        floor = CampusRepository(db).create_floor(data)
        detail["floor_id"] = floor["id"]
        building = CampusRepository(db).get_building(floor["building_id"])
        postgis = PostGISService()
        postgis.sync_floor({
            "id": f"{floor['building_id']}_{floor['id']}",
            "organization_id": building.get("organization_id"),
            "campus_id": building.get("campus_id"),
            "building_id": floor["building_id"],
            "floor_id": floor["id"],
            "floor_index": floor.get("floor_index"),
            "display_name": floor.get("display_name"),
            "floor_plan_url": floor.get("floor_plan_url"),
            "floor_plan_scale": floor.get("floor_plan_scale"),
            "floor_plan_origin_x": floor.get("floor_plan_origin_x"),
            "floor_plan_origin_y": floor.get("floor_plan_origin_y"),
            "floor_plan_bounds": floor.get("floor_plan_bounds"),
        })
        postgis.sync_building({
            "id": building["id"],
            "campus_id": building.get("campus_id"),
            "organization_id": building.get("organization_id"),
            "name": building.get("name"),
            "short_name": building.get("short_name"),
            "address": building.get("address"),
            "origin_lat": building.get("origin_lat"),
            "origin_lng": building.get("origin_lng"),
            "origin_bearing": building.get("origin_bearing"),
            "floor_count": building.get("floor_count"),
        })
    return floor


@router.get("/{floor_id}", response_model=Floor)
def get_floor(floor_id: str, db: Database = Depends(get_db)):
    repo = CampusRepository(db)
    try:
        floor = repo.get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        building = repo.get_building(floor["building_id"])
    except BuildingNotFound:
        return floor
    return _resolve_floor_origin(floor, building)


@router.patch("/{floor_id}", response_model=Floor)
def update_floor(
    floor_id: str,
    data: FloorUpdate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Reposition / rotate / resize a single floor. Stores a per-floor origin
    override and recomputes only that floor's spaces."""
    repo = CampusRepository(db)
    try:
        floor = repo.get_floor(floor_id)
        building = repo.get_building(floor["building_id"])
    except (FloorNotFound, BuildingNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = building.get("organization_id") if isinstance(building, dict) else None
    require_org_match(principal, org_id)

    with audit_action("update_floor", principal, organization_id=org_id) as detail:
        detail["floor_id"] = floor_id
        try:
            result = repo.update_floor(floor_id, data)
        except FloorNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))

        updated_floor = result["floor"]
        updated_spaces = result["updated_spaces"]
        detail["spaces_recomputed"] = len(updated_spaces)

        pg = PostGISService()
        for space in updated_spaces:
            pg.sync_space_geometry(space)

    return _resolve_floor_origin(updated_floor, building)


@router.get("/{floor_id}/spaces")
def list_spaces(floor_id: str, db: Database = Depends(get_db)):
    try:
        CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return SpaceRepository(db).get_floor_spaces(floor_id)


@router.get("/{floor_id}/display")
def floor_display(floor_id: str, db: Database = Depends(get_db)):
    """
    Return all spaces with polygons for iOS map overlay rendering.
    """
    repo = CampusRepository(db)
    try:
        floor = repo.get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    building = None
    if floor.get("building_id"):
        try:
            building = repo.get_building(floor["building_id"])
        except BuildingNotFound:
            building = None
    resolved = _resolve_floor_origin(floor, building or {})
    g_lat = resolved.get("origin_lat")
    g_lng = resolved.get("origin_lng")
    g_bearing = resolved.get("origin_bearing") or 0.0
    g_scale = resolved.get("scale_factor") or 1.0

    def _stamp_global(s: dict) -> None:
        """Rewrite polygon_global + centroid_lat/lon from the floor's
        current georef. Only runs when we have an origin to project from
        and the space has local coords."""
        if g_lat is None or g_lng is None:
            return
        poly = s.get("polygon")
        if poly and len(poly) >= 3:
            try:
                s["polygon_global"] = polygon_local_to_global(
                    poly, g_lat, g_lng, g_bearing, g_scale,
                )
            except Exception:
                pass
        cx = s.get("centroid_x")
        cy = s.get("centroid_y")
        if cx is not None and cy is not None:
            try:
                lat, lng = local_to_global_coordinates(
                    cx, cy, g_lat, g_lng, g_bearing, g_scale,
                )
                s["centroid_lat"] = lat
                s["centroid_lon"] = lng
                s["centroid_lng"] = lng
            except Exception:
                pass

    postgis = PostGISService()
    pg_spaces = postgis.get_floor_spaces(floor_id) or []
    pg_ids = {s["id"] for s in pg_spaces if s.get("id")}

    neo4j_spaces = SpaceRepository(db).get_floor_display(floor_id) or []

    render_order_by_id = {
        s["id"]: s.get("render_order")
        for s in neo4j_spaces
        if s.get("id") and s.get("render_order") is not None
    }
    for s in pg_spaces:
        ro = render_order_by_id.get(s.get("id"))
        if ro is not None:
            s["render_order"] = ro
        _stamp_global(s)

    missing = [s for s in neo4j_spaces if s.get("id") and s["id"] not in pg_ids]
    def _z_key(s: dict) -> int:
        ro = s.get("render_order")
        try:
            return int(ro) if ro is not None else 0
        except (TypeError, ValueError):
            return 0
    extras = []
    for s in missing:
        item = {
            "id": s["id"],
            "display_name": s.get("display_name"),
            "space_type": s.get("space_type"),
            "centroid_x": s.get("centroid_x"),
            "centroid_y": s.get("centroid_y"),
            "centroid_lat": s.get("centroid_lat"),
            "centroid_lon": s.get("centroid_lng"),  # iOS field name is centroid_lon
            "polygon": s.get("polygon"),
            "polygon_global": s.get("polygon_global"),
            "is_accessible": s.get("is_accessible", True),
            "is_navigable": s.get("is_navigable", True),
            "capacity": s.get("capacity"),
            "render_order": s.get("render_order"),
        }
        _stamp_global(item)
        extras.append(item)
    return sorted([*pg_spaces, *extras], key=_z_key)


@router.get("/{floor_id}/connections")
def floor_connections(floor_id: str, db: Database = Depends(get_db)):
    try:
        CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    rows = ConnectionRepository(db).list_connections_for_floor(floor_id)

    needs_positions = [
        (r["from_id"], r["to_id"]) for r in rows
        if r.get("door_id") is None
        and (r.get("door_cx") is None or r.get("door_cy") is None)
    ]
    if needs_positions:
        positions = PostGISService().get_direct_edge_door_positions(needs_positions)
        if positions:
            for r in rows:
                if r.get("door_id") is not None:
                    continue
                pos = positions.get((r["from_id"], r["to_id"]))
                if pos:
                    r["door_cx"], r["door_cy"] = pos[0], pos[1]
    return rows


@router.patch("/{floor_id}/connections/door")
def update_connection_door(
    floor_id: str,
    payload: dict,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Persist a door box position the editor dragged along the wall.
    """
    try:
        CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        from_id = str(payload["from_space_id"])
        to_id = str(payload["to_space_id"])
        dcx = float(payload["door_cx"])
        dcy = float(payload["door_cy"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Body must include from_space_id, to_space_id, door_cx, door_cy ({exc})",
        )
    ok = PostGISService().update_connection_door_position(from_id, to_id, dcx, dcy)
    return {"updated": bool(ok)}


@router.get("/{floor_id}/geometry")
def floor_geometry(floor_id: str, db: Database = Depends(get_db)):
    """
    Get floor geometry for rendering in iOS app.
    Returns all spaces with polygons, centroids, and metadata optimized for floor plan rendering.
    """
    try:
        floor = CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    try:
        spaces = SpaceRepository(db).get_floor_spaces(floor_id)

        rooms = []
        for space in spaces:
            room_data = {
                "id": space["id"],
                "name": space["display_name"],
                "type": space.get("space_type", "unknown"),
                "centroid": {"x": space.get("centroid_x"), "y": space.get("centroid_y")},
                "polygon": space.get("polygon"),
                "width_m": space.get("width_m"),
                "length_m": space.get("length_m"),
                "area_m2": space.get("area_m2"),
                "is_accessible": space.get("is_accessible", True),
                "is_navigable": space.get("is_navigable", True),
                "capacity": space.get("capacity"),
                "metadata": space.get("metadata", {}),
            }
            rooms.append(room_data)

        return {
            "floor": floor,
            "rooms": rooms,
            "count": len(rooms),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching floor geometry: {str(e)}")


@router.get("/{floor_id}/map-overlay")
def floor_map_overlay(floor_id: str, db: Database = Depends(get_db)):
    """
    Get floor plan data for map overlay in iOS app.
    Returns floor plan bounds, scale, and origin for MapKit overlay rendering.
    """
    try:
        floor = CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    try:
        postgis = PostGISService()
        floor_plan_data = postgis.get_floor_plan(f"{floor['building_id']}_{floor_id}")

        if not floor_plan_data:
            floor_plan_data = {
                "floor_plan_scale": floor.get("floor_plan_scale") or 1.0,
                "floor_plan_origin_x": floor.get("floor_plan_origin_x") or 0.0,
                "floor_plan_origin_y": floor.get("floor_plan_origin_y") or 0.0,
                "bounds": None,
            }

        building = CampusRepository(db).get_building(floor["building_id"])

        return {
            "floor_id": floor_id,
            "building_id": floor["building_id"],
            "floor_index": floor.get("floor_index"),
            "display_name": floor.get("display_name"),
            "building_origin": {
                "lat": building.get("origin_lat"),
                "lng": building.get("origin_lng"),
                "bearing": building.get("origin_bearing"),
            },
            "floor_plan": {
                "scale": floor_plan_data.get("floor_plan_scale", 1.0),
                "origin_x": floor_plan_data.get("floor_plan_origin_x", 0.0),
                "origin_y": floor_plan_data.get("floor_plan_origin_y", 0.0),
                "url": floor.get("floor_plan_url"),
                "bounds": floor_plan_data.get("bounds"),
            },
            "spaces_count": len(SpaceRepository(db).get_floor_spaces(floor_id)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching floor map overlay data: {str(e)}")


@router.delete("/{floor_id}", status_code=204)
def delete_floor(
    floor_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    try:
        existing = CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = existing.get("organization_id") if isinstance(existing, dict) else None
    require_org_match(principal, org_id)
    with audit_action("delete_floor", principal, organization_id=org_id) as detail:
        detail["floor_id"] = floor_id
        try:
            result = CampusRepository(db).delete_floor(floor_id)
        except FloorNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        PostGISService().delete_floor_cascade(
            building_id=result["building_id"],
            floor_id=result["floor_id"],
            space_ids=result["space_ids"],
        )
