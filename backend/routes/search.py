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
