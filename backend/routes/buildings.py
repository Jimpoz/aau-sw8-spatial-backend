from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import BuildingNotFound
from models.campus import Building, BuildingCreate
from repositories.campus_repo import CampusRepository

router = APIRouter(prefix="/buildings", tags=["buildings"])


@router.post("", response_model=Building, status_code=201)
def create_building(data: BuildingCreate, db: Database = Depends(get_db)):
    return CampusRepository(db).create_building(data)


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
