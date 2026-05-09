from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File, Form
from db import Database, get_db
from core.auth_principal import Principal, get_principal, require_org_match, require_role
from core.exceptions import CampusNotFound
from models.campus import Building, CampusCreate, Campus, VisibleCampus
from models.map_import import MapImportSchema
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from services.audit_service import audit_action
from services.dxf_import_service import DxfImportService, parse_layer_mapping
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
    request: Request,
    db: Database = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Flat list of campuses the caller can see — their org's campuses plus any public ones."""
    # Diagnostic logs to determine whether the gateway injected identity headers
    print(
        "[VISIBLE_CAMPUSES] request headers:",
        "x-user-id=" + str(request.headers.get("x-user-id")),
        "x-org-id=" + str(request.headers.get("x-org-id")),
        "x-org-ids=" + str(request.headers.get("x-org-ids")),
        "authorization-present=" + str(bool(request.headers.get("authorization"))),
    )
    print(
        f"[VISIBLE_CAMPUSES] principal: user_id={principal.user_id} active_org={principal.org_id} "
        f"org_ids={list(principal.org_ids)} role={principal.role} is_mapmaker={principal.is_mapmaker}"
    )
    # Prefer principal.org_ids; fall back to caller-supplied header when empty.
    org_ids = list(principal.org_ids)
    if not org_ids:
        hdr = request.headers.get("x-org-ids") or request.headers.get("x-org-id")
        if hdr:
            try:
                import json as _json

                parsed = _json.loads(hdr)
                if isinstance(parsed, list):
                    org_ids = parsed
                elif isinstance(parsed, str):
                    org_ids = [parsed]
            except Exception:
                # Not JSON — allow comma-separated or single value
                if isinstance(hdr, str):
                    org_ids = [s.strip() for s in hdr.split(",") if s.strip()]
                else:
                    org_ids = [hdr]

    rows = CampusRepository(db).list_visible_campuses(org_ids=org_ids)
    print(
        f"[VISIBLE_CAMPUSES] used_org_ids={org_ids} returned {len(rows)} campuses; "
        f"org_ids_in_rows={[r.get('organization_id') for r in rows]}; ids={[r.get('id') for r in rows]}"
    )
    return rows


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


@router.post("/import-dxf")
async def import_dxf(
    file: UploadFile = File(...),
    campus_id: str = Form(...),
    campus_name: str = Form(...),
    building_id: str = Form(...),
    building_name: str = Form(...),
    floor_id: str = Form(...),
    floor_index: int = Form(0),
    floor_display_name: str = Form("Ground"),
    organization_id: Optional[str] = Form(None),
    organization_name: Optional[str] = Form(None),
    origin_lat: Optional[float] = Form(None),
    origin_lng: Optional[float] = Form(None),
    origin_bearing: float = Form(0.0),
    layer_mapping: Optional[str] = Form(None),
    dry_run: bool = Form(False),
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Upload a DWG / DXF floor plan and convert + import it in one shot.

    The flow is:
        upload  →  DxfImportService.parse  →  MapImportSchema dict
                →  (dry_run? return preview)
                →  ImportService.import_map  →  Neo4j + PostGIS (atomic)
                →  GdsService.refresh_projection

    Same dual-write pipeline that JSON imports use; the only thing the DXF
    leg adds is the parser. DWG uploads transcode to DXF first via
    ODAFileConverter (returns 415 if the binary is not on PATH).

    `layer_mapping` is an optional JSON object (sent as a form field
    string) that overrides the default heuristic for layer→SpaceType
    classification. Example: `{"WALLS_OFFICE_1": "ROOM_OFFICE"}`.

    `dry_run=true` parses the file and returns the schema preview plus
    parsing warnings WITHOUT writing to either database. Use it to
    verify the layer-mapping result before committing the import."""
    require_org_match(principal, organization_id)

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    try:
        layer_overrides = parse_layer_mapping(layer_mapping)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        schema_dict = DxfImportService().parse(
            file_bytes,
            file.filename or "upload.dxf",
            campus_id=campus_id,
            campus_name=campus_name,
            building_id=building_id,
            building_name=building_name,
            floor_id=floor_id,
            floor_index=floor_index,
            floor_display_name=floor_display_name,
            organization_id=organization_id,
            organization_name=organization_name,
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            origin_bearing=origin_bearing,
            layer_mapping=layer_overrides,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DXF parse failed: {exc}")

    warnings = schema_dict.get("_warnings", [])
    classification = schema_dict.get("_classification_summary", {})
    spaces_in_floor = schema_dict["campus"]["buildings"][0]["floors"][0]["spaces"]
    rooms_detected = len(spaces_in_floor)

    try:
        schema = MapImportSchema(**schema_dict)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Schema validation failed: {exc}")

    if dry_run:
        id_to_centroid = {s["id"]: (s.get("centroid_x"), s.get("centroid_y")) for s in spaces_in_floor}
        preview_conns = []
        for c in (schema_dict.get("campus", {}).get("connections") or []):
            from_id = c.get("from_space_id") or c.get("from_id") or c.get("from")
            to_id = c.get("to_space_id") or c.get("to_id") or c.get("to")
            if not from_id or not to_id:
                continue
            from_xy = id_to_centroid.get(from_id, (None, None))
            to_xy = id_to_centroid.get(to_id, (None, None))
            preview_conns.append({
                "from_id": from_id,
                "to_id": to_id,
                "from_cx": from_xy[0],
                "from_cy": from_xy[1],
                "to_cx": to_xy[0],
                "to_cy": to_xy[1],
                "connection_type": c.get("connection_type"),
                "is_accessible": c.get("is_accessible", True),
            })

        return {
            "dry_run": True,
            "rooms_detected": rooms_detected,
            "classification_summary": classification,
            "warnings": warnings,
            "preview_schema": schema_dict,
            "preview_spaces": [
                {"id": s["id"], "display_name": s["display_name"], "space_type": s["space_type"]}
                for s in spaces_in_floor
            ],
            "preview_connections": preview_conns,
        }

    from services.audit_service import write_audit_log
    try:
        result = ImportService(db).import_map(schema)
        GdsService(db).refresh_projection()
        write_audit_log(
            action="import_dxf",
            success=True,
            subject_user_id=principal.user_id,
            organization_id=organization_id,
            detail={
                "campus_id": campus_id,
                "filename": file.filename,
                "spaces_imported": result.get("spaces_imported"),
                "rooms_detected": rooms_detected,
                "warnings_count": len(warnings),
            },
        )
        return {
            **result,
            "rooms_detected": rooms_detected,
            "classification_summary": classification,
            "warnings": warnings,
        }
    except Exception as exc:
        write_audit_log(
            action="import_dxf",
            success=False,
            subject_user_id=principal.user_id,
            organization_id=organization_id,
            detail={"campus_id": campus_id, "filename": file.filename, "error": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{campus_id}/export")
def export_map(campus_id: str, db: Database = Depends(get_db)):
    """Export a campus as a JSON document that can be re-imported through
    `POST /campuses/{campus_id}/import` without modification — the shape
    matches `MapImportSchema` exactly. The `connections` array carries
    pairwise edges in the `{from_space_id, to_space_id, connection_type,
    is_accessible}` shape the import expects, sourced from the
    PostGIS-mirrored `space_connections` table when available and
    derived from Neo4j `:CONNECTS_TO` edges otherwise."""

    try:
        campus = CampusRepository(db).get_campus(campus_id)
    except CampusNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    campus_repo = CampusRepository(db)
    space_repo = SpaceRepository(db)
    DOOR_LIKE_TYPES = {
        "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED",
        "DOOR_EMERGENCY", "PASSAGE",
    }

    _CAMPUS_STRIP = {"created_at", "updated_at"}
    _BUILDING_STRIP = {"created_at", "updated_at", "campus_id"}
    _FLOOR_STRIP = {"created_at", "updated_at", "building_id"}

    def _strip(d: dict, drop: set[str]) -> dict:
        return {k: v for k, v in d.items() if k not in drop and v is not None}

    def _clean_space(s: dict) -> dict:
        DROP = {
            "campus_id", "building_id", "floor_id", "floor_index",
            "embedding", "tags_text", "traversal_cost",
            "centroid_lat", "centroid_lon", "polygon_global",
            "created_at", "updated_at",
        }
        out = {k: v for k, v in s.items() if k not in DROP}
        if "subspaces" in out and isinstance(out["subspaces"], list):
            out["subspaces"] = [_clean_space(sub) for sub in out["subspaces"]]
        return out

    buildings_out = []
    for building in campus_repo.list_buildings(campus_id):
        floors_out = []
        for floor in campus_repo.list_floors(building["id"]):
            raw_spaces = space_repo.get_floor_spaces_with_subspaces(floor["id"])
            kept_spaces = [
                _clean_space(s)
                for s in raw_spaces
                if s.get("space_type") not in DOOR_LIKE_TYPES
            ]
            floors_out.append({**_strip(floor, _FLOOR_STRIP), "spaces": kept_spaces})
        buildings_out.append({**_strip(building, _BUILDING_STRIP), "floors": floors_out})

    connections_out = PostGISService().list_campus_connections(campus_id)
    if not connections_out:
        connections_out = _derive_connections_from_neo4j(db, campus_id)

    for c in connections_out:
        raw_ct = c.get("connection_type")
        ct = (str(raw_ct).upper() if raw_ct is not None else "OPEN")

        if ct in DOOR_LIKE_TYPES:
            if ct.startswith("DOOR_"):
                c["door_type"] = ct.split("DOOR_", 1)[1]
            else:
                c["door_type"] = None
            c["connection_type"] = "DOOR"
        else:
            c["connection_type"] = ct

        if "is_accessible" not in c:
            c["is_accessible"] = True
        c.setdefault("requires_access_level", None)
        c.setdefault("transition_time_s", None)
        c.setdefault("weight_override", None)

    organization = None
    org_id = campus.get("organization_id") if isinstance(campus, dict) else None
    if org_id:
        from repositories.campus_repo import OrganizationRepository
        from core.exceptions import OrganizationNotFound
        try:
            org_raw = OrganizationRepository(db).get_organization(org_id)
            organization = {
                "id": org_raw.get("id"),
                "name": org_raw.get("name"),
                "entity_type": org_raw.get("entity_type") or "OTHER",
                "description": org_raw.get("description"),
            }
            organization = {k: v for k, v in organization.items() if v is not None}
        except OrganizationNotFound:
            organization = None

    return {
        "schema_version": "1.0",
        "organization": organization,
        "campus": {
            **_strip(campus, _CAMPUS_STRIP),
            "buildings": buildings_out,
            "outdoor_spaces": [],
            "connections": connections_out,
        },
    }


def _derive_connections_from_neo4j(db, campus_id: str) -> list[dict]:
    """Fallback path: compute the import-compatible connections list
    directly from Neo4j `:CONNECTS_TO` edges."""
    rows = db.execute(
        """
        MATCH (a:Space {campus_id: $campus_id})-[:CONNECTS_TO]->(b:Space)
        OPTIONAL MATCH (fa:Floor)-[:HAS_SPACE]->(a)
        OPTIONAL MATCH (fb:Floor)-[:HAS_SPACE]->(b)
        RETURN a.id AS from_id, b.id AS to_id,
               a.space_type AS a_type, b.space_type AS b_type,
               coalesce(a.is_accessible, true) AND coalesce(b.is_accessible, true) AS is_accessible,
               fa.floor_index AS a_floor, fb.floor_index AS b_floor
        """,
        {"campus_id": campus_id},
    )
    DOOR_LIKE = {"DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED", "DOOR_EMERGENCY", "PASSAGE"}
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in rows:
        from_id, to_id = r["from_id"], r["to_id"]
        if (from_id, to_id) in seen:
            continue
        seen.add((from_id, to_id))
        a_type = r["a_type"] or ""
        b_type = r["b_type"] or ""
        a_floor = r.get("a_floor")
        b_floor = r.get("b_floor")
        if a_type == "STAIRCASE" and b_type == "STAIRCASE":
            ct = "STAIRCASE_UP" if (a_floor or 0) <= (b_floor or 0) else "STAIRCASE_DOWN"
        elif a_type == "ELEVATOR" and b_type == "ELEVATOR":
            ct = "ELEVATOR_UP" if (a_floor or 0) <= (b_floor or 0) else "ELEVATOR_DOWN"
        elif a_type == "ESCALATOR" or b_type == "ESCALATOR":
            ct = "ESCALATOR"
        elif a_type in DOOR_LIKE or b_type in DOOR_LIKE:
            ct = "DOOR"
        else:
            ct = "OPEN"
        out.append({
            "from_space_id": from_id,
            "to_space_id": to_id,
            "connection_type": ct,
            "is_accessible": bool(r["is_accessible"]) if r["is_accessible"] is not None else True,
        })
    return out


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
