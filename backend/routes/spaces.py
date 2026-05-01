from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.auth_principal import Principal, require_org_match, require_role
from core.exceptions import CampusNotFound, SpaceNotFound
from models.space import Space, SpaceCreate, SpaceUpdate
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from repositories.campus_repo import CampusRepository
from services.audit_service import audit_action
from services.postgis_service import PostGISService
from services.space_sync import build_space_sync_payload

router = APIRouter(prefix="/spaces", tags=["spaces"])


def _resolve_space_org(db: Database, data: SpaceCreate) -> str | None:
    """Pick the org id a new space belongs to."""
    if data.organization_id:
        return data.organization_id
    if data.campus_id:
        try:
            campus = CampusRepository(db).get_campus(data.campus_id)
            return campus.get("organization_id") if isinstance(campus, dict) else None
        except CampusNotFound:
            return None
    return None


@router.post("", response_model=Space, status_code=201)
def create_space(
    data: SpaceCreate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    org_id = _resolve_space_org(db, data)
    require_org_match(principal, org_id)
    with audit_action("create_space", principal, organization_id=org_id) as detail:
        detail["campus_id"] = data.campus_id
        space = SpaceRepository(db).create_space(data)
        detail["space_id"] = space.get("id") if isinstance(space, dict) else None
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
def update_space(
    space_id: str,
    data: SpaceUpdate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    try:
        existing = SpaceRepository(db).get_space(space_id)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = existing.get("organization_id") if isinstance(existing, dict) else None
    require_org_match(principal, org_id)
    with audit_action("update_space", principal, organization_id=org_id) as detail:
        detail["space_id"] = space_id
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
def delete_space(
    space_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    try:
        existing = SpaceRepository(db).get_space(space_id)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = existing.get("organization_id") if isinstance(existing, dict) else None
    require_org_match(principal, org_id)
    with audit_action("delete_space", principal, organization_id=org_id) as detail:
        detail["space_id"] = space_id
        try:
            door_ids = SpaceRepository(db).delete_space(space_id)
        except SpaceNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        postgis = PostGISService()
        for door_id in door_ids:
            postgis.delete_connection_group(door_id)
            postgis.delete_space(door_id)
        postgis.delete_edges_for_space(space_id)
        postgis.delete_space(space_id)


@router.get("/{space_id}/connections")
def list_connections_for_space(space_id: str, db: Database = Depends(get_db)):
    return ConnectionRepository(db).list_connections_for_space(space_id)
