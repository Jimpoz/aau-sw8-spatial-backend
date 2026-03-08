from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import SpaceNotFound
from models.space import Space, SpaceCreate, SpaceUpdate
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository

router = APIRouter(prefix="/spaces", tags=["spaces"])


@router.post("", response_model=Space, status_code=201)
def create_space(data: SpaceCreate, db: Database = Depends(get_db)):
    return SpaceRepository(db).create_space(data)


@router.get("/{space_id}", response_model=Space)
def get_space(space_id: str, db: Database = Depends(get_db)):
    try:
        return SpaceRepository(db).get_space(space_id)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{space_id}", response_model=Space)
def update_space(space_id: str, data: SpaceUpdate, db: Database = Depends(get_db)):
    try:
        return SpaceRepository(db).update_space(space_id, data)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{space_id}", status_code=204)
def delete_space(space_id: str, db: Database = Depends(get_db)):
    try:
        SpaceRepository(db).delete_space(space_id)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{space_id}/connections")
def list_connections_for_space(space_id: str, db: Database = Depends(get_db)):
    return ConnectionRepository(db).list_connections_for_space(space_id)
