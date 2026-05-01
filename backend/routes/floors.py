from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.auth_principal import Principal, require_org_match, require_role
from core.exceptions import BuildingNotFound, FloorNotFound
from models.campus import Floor, FloorCreate
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from services.audit_service import audit_action
from services.postgis_service import PostGISService

router = APIRouter(prefix="/floors", tags=["floors"])


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
    try:
        return CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    Reads from PostGIS (Supabase) when available; falls back to Neo4j.
    Returns a flat list matching the iOS SpaceDisplayItem decoder.
    """
    try:
        CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    postgis = PostGISService()
    spaces = postgis.get_floor_spaces(floor_id)

    if spaces:
        return spaces

    neo4j_spaces = SpaceRepository(db).get_floor_display(floor_id)
    return [
        {
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
        }
        for s in neo4j_spaces
    ]


@router.get("/{floor_id}/connections")
def floor_connections(floor_id: str, db: Database = Depends(get_db)):
    try:
        CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ConnectionRepository(db).list_connections_for_floor(floor_id)


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
