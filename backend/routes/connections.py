from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from models.connection import Connection, ConnectionCreate
from repositories.connection_repo import ConnectionRepository

router = APIRouter(prefix="/connections", tags=["connections"])


@router.post("", response_model=Connection, status_code=201)
def create_connection(data: ConnectionCreate, db: Database = Depends(get_db)):
    result = ConnectionRepository(db).create_connection(data)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Space '{data.from_space_id}' or '{data.to_space_id}' not found",
        )
    return result


@router.get("/{from_space_id}/{to_space_id}", response_model=Connection)
def get_connection(from_space_id: str, to_space_id: str, db: Database = Depends(get_db)):
    result = ConnectionRepository(db).get_connection(from_space_id, to_space_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No connection from '{from_space_id}' to '{to_space_id}'",
        )
    return result


@router.delete("/{from_space_id}/{to_space_id}", status_code=204)
def delete_connection(from_space_id: str, to_space_id: str, db: Database = Depends(get_db)):
    deleted = ConnectionRepository(db).delete_connection(from_space_id, to_space_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"No connection from '{from_space_id}' to '{to_space_id}'",
        )
