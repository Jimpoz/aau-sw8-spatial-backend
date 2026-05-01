from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from core.auth_principal import Principal, get_principal, require_org_match, require_role
from core.exceptions import CampusNotFound
from models.campus import Building, CampusCreate, Campus, VisibleCampus
from models.map_import import MapImportSchema
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from services.audit_service import audit_action
from services.import_service import ImportService
from services.gds_service import GdsService
from services.postgis_service import PostGISService

router = APIRouter(prefix="/campuses", tags=["campuses"])


@router.get("", response_model=list[Campus])
def list_campuses(
    organization_id: str | None = None, db: Database = Depends(get_db)
):
    return CampusRepository(db).list_campuses(organization_id=organization_id)


@router.get("/visible", response_model=list[VisibleCampus])
def list_visible_campuses(
    db: Database = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Flat list of campuses the caller can see — their org's campuses plus any public ones."""
    return CampusRepository(db).list_visible_campuses(org_id=principal.org_id)


@router.post("", response_model=Campus, status_code=201)
def create_campus(
    data: CampusCreate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    require_org_match(principal, data.organization_id)
    with audit_action("create_campus", principal, organization_id=data.organization_id) as detail:
        detail["campus_id"] = data.id
        detail["name"] = data.name
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
def delete_campus(
    campus_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    try:
        existing = CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    org_id = existing.get("organization_id") if isinstance(existing, dict) else None
    require_org_match(principal, org_id)
    with audit_action("delete_campus", principal, organization_id=org_id) as detail:
        detail["campus_id"] = campus_id
        CampusRepository(db).delete_campus(campus_id)
        PostGISService().delete_campus(campus_id)


@router.post("/{campus_id}/import")
def import_map(
    campus_id: str,
    schema: MapImportSchema,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    if schema.campus.id != campus_id:
        raise HTTPException(
            status_code=422,
            detail=f"campus.id in body ('{schema.campus.id}') must match URL campus_id ('{campus_id}')",
        )

    try:
        existing = CampusRepository(db).get_campus(campus_id)
        target_org_id = existing.get("organization_id") if isinstance(existing, dict) else None
    except CampusNotFound:
        target_org_id = schema.campus.organization_id or (
            schema.organization.id if schema.organization else None
        )
    require_org_match(principal, target_org_id)

    from services.audit_service import write_audit_log
    try:
        result = ImportService(db).import_map(schema)
        GdsService(db).refresh_projection()
        write_audit_log(
            action="import_map",
            success=True,
            subject_user_id=principal.user_id,
            organization_id=target_org_id,
            detail={
                "campus_id": campus_id,
                "spaces_imported": result.get("spaces_imported"),
                "connections_imported": result.get("connections_imported"),
            },
        )
        return result
    except Exception as e:
        write_audit_log(
            action="import_map",
            success=False,
            subject_user_id=principal.user_id,
            organization_id=target_org_id,
            detail={"campus_id": campus_id, "error": str(e)},
        )
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


@router.get("/{campus_id}/buildings", response_model=list[Building])
def list_campus_buildings(campus_id: str, db: Database = Depends(get_db)):
    """Lightweight building list for the campus — used by the iOS map view to
    locate which building the user just zoomed onto without pulling the full
    map export."""
    try:
        CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return CampusRepository(db).list_buildings(campus_id)
