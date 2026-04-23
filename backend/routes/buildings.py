from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import BuildingNotFound
from models.campus import Building, BuildingCreate
from repositories.campus_repo import CampusRepository
from services.postgis_service import PostGISService

router = APIRouter(prefix="/buildings", tags=["buildings"])


@router.post("", response_model=Building, status_code=201)
def create_building(data: BuildingCreate, db: Database = Depends(get_db)):
    building = CampusRepository(db).create_building(data)
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
def delete_building(building_id: str, db: Database = Depends(get_db)):
    try:
        result = CampusRepository(db).delete_building(building_id)
    except BuildingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    PostGISService().delete_building_cascade(
        building_id=result["building_id"],
        space_ids=result["space_ids"],
        floor_ids=result["floor_ids"],
    )
