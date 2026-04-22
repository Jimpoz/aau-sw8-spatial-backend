from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import SpaceNotFound
from models.space import Space, SpaceCreate, SpaceUpdate
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from repositories.campus_repo import CampusRepository
from services.postgis_service import PostGISService
from services.space_sync import build_space_sync_payload

router = APIRouter(prefix="/spaces", tags=["spaces"])


@router.post("", response_model=Space, status_code=201)
def create_space(data: SpaceCreate, db: Database = Depends(get_db)):
    space = SpaceRepository(db).create_space(data)
    PostGISService().sync_space(
        build_space_sync_payload(space, CampusRepository(db))
    )
    return space


@router.get("/{space_id}", response_model=Space)
def get_space(space_id: str, db: Database = Depends(get_db)):
    try:
        return SpaceRepository(db).get_space(space_id)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{space_id}", response_model=Space)
def update_space(space_id: str, data: SpaceUpdate, db: Database = Depends(get_db)):
    try:
        space = SpaceRepository(db).update_space(space_id, data)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    postgis = PostGISService()
    postgis.sync_space(
        build_space_sync_payload(space, CampusRepository(db))
    )
    if space.get("is_accessible") is not None:
        postgis.update_connection_group_access(space_id, bool(space["is_accessible"]))
    return space


@router.delete("/{space_id}", status_code=204)
def delete_space(space_id: str, db: Database = Depends(get_db)):
    try:
        SpaceRepository(db).delete_space(space_id)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    postgis = PostGISService()
    postgis.delete_edges_for_space(space_id)
    postgis.delete_space(space_id)


@router.get("/{space_id}/connections")
def list_connections_for_space(space_id: str, db: Database = Depends(get_db)):
    return ConnectionRepository(db).list_connections_for_space(space_id)
