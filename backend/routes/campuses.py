from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.exceptions import CampusNotFound
from models.campus import CampusCreate, Campus
from models.map_import import MapImportSchema
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from services.import_service import ImportService
from services.gds_service import GdsService
from services.postgis_service import PostGISService

router = APIRouter(prefix="/campuses", tags=["campuses"])


@router.get("", response_model=list[Campus])
def list_campuses(
    organization_id: str | None = None, db: Database = Depends(get_db)
):
    return CampusRepository(db).list_campuses(organization_id=organization_id)


@router.post("", response_model=Campus, status_code=201)
def create_campus(data: CampusCreate, db: Database = Depends(get_db)):
    campus = CampusRepository(db).create_campus(data)
    PostGISService().sync_campus({
        "id": data.id,
        "organization_id": data.organization_id,
        "name": data.name,
        "description": data.description,
    })
    return campus


@router.get("/{campus_id}", response_model=Campus)
def get_campus(campus_id: str, db: Database = Depends(get_db)):
    try:
        return CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{campus_id}", status_code=204)
def delete_campus(campus_id: str, db: Database = Depends(get_db)):
    try:
        CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    CampusRepository(db).delete_campus(campus_id)
    PostGISService().delete_campus(campus_id)


@router.post("/{campus_id}/import")
def import_map(campus_id: str, schema: MapImportSchema, db: Database = Depends(get_db)):
    if schema.campus.id != campus_id:
        raise HTTPException(
            status_code=422,
            detail=f"campus.id in body ('{schema.campus.id}') must match URL campus_id ('{campus_id}')",
        )
    try:
        result = ImportService(db).import_map(schema)
        GdsService(db).refresh_projection()
        return result
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/{campus_id}/export")
def export_map(campus_id: str, db: Database = Depends(get_db)):
    try:
        campus = CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    campus_repo = CampusRepository(db)
    space_repo = SpaceRepository(db)
    buildings_out = []
    for building in campus_repo.list_buildings(campus_id):
        floors_out = []
        for floor in campus_repo.list_floors(building["id"]):
            spaces_out = space_repo.get_floor_spaces_with_subspaces(floor["id"])
            floors_out.append({**floor, "spaces": spaces_out})
        buildings_out.append({**building, "floors": floors_out})

    # Export connection nodes (doors/passages) with their connected spaces
    conn_types = [
        "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED", "DOOR_EMERGENCY", "PASSAGE",
        "STAIRCASE", "ELEVATOR", "ESCALATOR", "RAMP",
    ]
    conn_result = db.execute(
        """
        MATCH (conn:Space {campus_id: $campus_id})
        WHERE conn.space_type IN $conn_types
        OPTIONAL MATCH (conn)-[:CONNECTS_TO]->(neighbor:Space)
        WHERE NOT neighbor.space_type IN $conn_types
        RETURN conn, collect(DISTINCT neighbor.id) AS connected_ids
        """,
        {"campus_id": campus_id, "conn_types": conn_types},
    )
    connections_out = []
    for row in conn_result:
        node = row["conn"]
        connections_out.append({
            "id": node.get("id"),
            "display_name": node.get("display_name", "Door"),
            "space_type": node.get("space_type"),
            "connects": row["connected_ids"],
            "centroid_x": node.get("centroid_x"),
            "centroid_y": node.get("centroid_y"),
            "is_accessible": node.get("is_accessible", True),
            "tags": node.get("tags", []),
        })

    organization = None
    org_id = campus.get("organization_id") if isinstance(campus, dict) else None
    if org_id:
        from repositories.campus_repo import OrganizationRepository
        from core.exceptions import OrganizationNotFound
        try:
            organization = OrganizationRepository(db).get_organization(org_id)
        except OrganizationNotFound:
            organization = None

    return {
        "schema_version": "1.0",
        "organization": organization,
        "campus": {
            **campus,
            "buildings": buildings_out,
            "connections": connections_out,
        },
    }


@router.get("/{campus_id}/search")
def search_spaces(campus_id: str, q: str, db: Database = Depends(get_db)):
    return SpaceRepository(db).search(campus_id, q)
