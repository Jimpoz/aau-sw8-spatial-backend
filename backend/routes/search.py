from fastapi import APIRouter, HTTPException, Depends, Query
from db import Database, get_db
from core.exceptions import CampusNotFound
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/campuses/{campus_id}/spaces")
def search_spaces(
    campus_id: str,
    q: str = Query(..., min_length=1),
    db: Database = Depends(get_db),
):
    try:
        CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return SpaceRepository(db).search(campus_id, q)


@router.get("/spaces")
def search_spaces_global(q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=200), db: Database = Depends(get_db)):
    """Global fulltext search across all campuses for navigable spaces."""
    return SpaceRepository(db).search_all(q, limit=limit)


@router.get("/nearest-space")
def nearest_space(lat: float = Query(...), lon: float = Query(...), limit: int = Query(1, ge=1, le=10), db: Database = Depends(get_db)):
    """Find nearest navigable spaces to the given lat/lon."""
    return SpaceRepository(db).nearest_space(lat, lon, limit=limit)
