from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import BuildingNotFound
from models.campus import Building, BuildingCreate
from repositories.campus_repo import CampusRepository

router = APIRouter(prefix="/buildings", tags=["buildings"])


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
