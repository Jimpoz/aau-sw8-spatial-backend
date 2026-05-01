from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.auth_principal import Principal, get_principal, require_org_match, require_role
from core.exceptions import BuildingNotFound, CampusNotFound
from models.campus import Building, BuildingCreate, VisibleBuilding
from repositories.campus_repo import CampusRepository
from services.audit_service import audit_action
from services.postgis_service import PostGISService

router = APIRouter(prefix="/buildings", tags=["buildings"])


@router.get("/visible", response_model=list[VisibleBuilding])
def list_visible_buildings(
    db: Database = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    return CampusRepository(db).list_visible_buildings(org_ids=list(principal.org_ids))


@router.post("", response_model=Building, status_code=201)
def create_building(
    data: BuildingCreate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    target_org_id = data.organization_id
    if target_org_id is None:
        try:
            campus = CampusRepository(db).get_campus(data.campus_id)
            target_org_id = campus.get("organization_id") if isinstance(campus, dict) else None
        except CampusNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
    require_org_match(principal, target_org_id)
    with audit_action("create_building", principal, organization_id=target_org_id) as detail:
        detail["campus_id"] = data.campus_id
        detail["name"] = data.name
        building = CampusRepository(db).create_building(data)
        detail["building_id"] = building["id"]
        PostGISService().sync_building({
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
    return building


@router.get("/{building_id}", response_model=Building)
def get_building(building_id: str, db: Database = Depends(get_db)):
    try:
        return CampusRepository(db).get_building(building_id)
    except BuildingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{building_id}/floors")
def list_floors(building_id: str, db: Database = Depends(get_db)):
    try:
        CampusRepository(db).get_building(building_id)
    except BuildingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return CampusRepository(db).list_floors(building_id)


@router.delete("/{building_id}", status_code=204)
def delete_building(
    building_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    try:
        existing = CampusRepository(db).get_building(building_id)
    except BuildingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = existing.get("organization_id") if isinstance(existing, dict) else None
    require_org_match(principal, org_id)
    with audit_action("delete_building", principal, organization_id=org_id) as detail:
        detail["building_id"] = building_id
        try:
            result = CampusRepository(db).delete_building(building_id)
        except BuildingNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        PostGISService().delete_building_cascade(
            building_id=result["building_id"],
            space_ids=result["space_ids"],
            floor_ids=result["floor_ids"],
        )
