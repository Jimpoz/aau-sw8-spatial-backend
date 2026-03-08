from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import FloorNotFound
from models.campus import Floor
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository

router = APIRouter(prefix="/floors", tags=["floors"])


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
    """Return all spaces with polygons and nested subspaces for frontend rendering."""
    try:
        floor = CampusRepository(db).get_floor(floor_id)
    except FloorNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    spaces = SpaceRepository(db).get_floor_display(floor_id)
    return {"floor": floor, "spaces": spaces}
