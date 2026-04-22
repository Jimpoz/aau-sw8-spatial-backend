from fastapi import APIRouter, HTTPException, Depends

from db import Database, get_db
from core.exceptions import OrganizationNotFound
from models.campus import (
    Organization,
    OrganizationCreate,
    Campus,
)
from repositories.campus_repo import OrganizationRepository
from services.postgis_service import PostGISService
from shared.models.enums import EntityType

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=list[Organization])
def list_organizations(db: Database = Depends(get_db)):
    return OrganizationRepository(db).list_organizations()


@router.post("", response_model=Organization, status_code=201)
def create_organization(data: OrganizationCreate, db: Database = Depends(get_db)):
    record = OrganizationRepository(db).create_organization(data)
    PostGISService().sync_organization({
        "id": data.id,
        "name": data.name,
        "entity_type": data.entity_type,
        "description": data.description,
    })
    return record


@router.get("/{organization_id}", response_model=Organization)
def get_organization(organization_id: str, db: Database = Depends(get_db)):
    try:
        return OrganizationRepository(db).get_organization(organization_id)
    except OrganizationNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{organization_id}/campuses", response_model=list[Campus])
def list_org_campuses(organization_id: str, db: Database = Depends(get_db)):
    try:
        OrganizationRepository(db).get_organization(organization_id)
    except OrganizationNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return OrganizationRepository(db).list_campuses(organization_id)


@router.delete("/{organization_id}", status_code=204)
def delete_organization(organization_id: str, db: Database = Depends(get_db)):
    try:
        OrganizationRepository(db).get_organization(organization_id)
    except OrganizationNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    OrganizationRepository(db).delete_organization(organization_id)
    PostGISService().delete_organization(organization_id)


@router.get("/enums/entity-types")
def entity_types():
    return {"entity_types": [t.value for t in EntityType]}
