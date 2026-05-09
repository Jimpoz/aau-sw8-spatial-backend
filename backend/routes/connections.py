import uuid

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from db import Database, get_db
from core.auth_principal import Principal, require_org_match, require_role
from models.connection import Connection, ConnectionCreate
from models.space import SpaceCreate, SpaceUpdate
from models.enums import CONN_SPACE_TYPES, SpaceType
from repositories.connection_repo import ConnectionRepository
from repositories.space_repo import SpaceRepository
from repositories.campus_repo import CampusRepository
from services.audit_service import audit_action
from services.geometry_service import (
    compute_traversal_cost,
    find_shared_edge_midpoint,
    local_to_global_coordinates,
)
from services.postgis_service import PostGISService
from services.space_sync import build_space_sync_payload

router = APIRouter(prefix="/connections", tags=["connections"])


@router.post("", response_model=Connection, status_code=201)
def create_connection(
    data: ConnectionCreate,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    space_repo = SpaceRepository(db)
    conn_repo = ConnectionRepository(db)

    # Look up both spaces to get centroids and context
    try:
        space_a = space_repo.get_space(data.from_space_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Space '{data.from_space_id}' not found")
    try:
        space_b = space_repo.get_space(data.to_space_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Space '{data.to_space_id}' not found")

    org_a = space_a.get("organization_id")
    org_b = space_b.get("organization_id")
    if org_a is not None and org_b is not None and org_a != org_b:
        raise HTTPException(status_code=422, detail="Cannot connect spaces from different organizations")
    org_id = org_a or org_b
    require_org_match(principal, org_id)

    with audit_action("create_connection", principal, organization_id=org_id) as detail:
        detail["from_space_id"] = data.from_space_id
        detail["to_space_id"] = data.to_space_id
        poly_a = space_a.get("polygon")
        poly_b = space_b.get("polygon")
        cx, cy = None, None

        if poly_a and poly_b:
            result = find_shared_edge_midpoint(poly_a, poly_b)
            if result:
                cx, cy = result

        if cx is None and cy is None:
            cx_a, cy_a = space_a.get("centroid_x"), space_a.get("centroid_y")
            cx_b, cy_b = space_b.get("centroid_x"), space_b.get("centroid_y")
            if all(v is not None for v in (cx_a, cy_a, cx_b, cy_b)):
                cx = (cx_a + cx_b) / 2.0
                cy = (cy_a + cy_b) / 2.0

        door_id = str(uuid.uuid4())
        detail["door_id"] = door_id

        traversal_cost = compute_traversal_cost(data.space_type.value, None, None, None)

        campus_id = space_a.get("campus_id")
        floor_a = space_a.get("floor_id")
        floor_b = space_b.get("floor_id")
        floor_id = floor_a if floor_a == floor_b else None

        # Create the intermediate door/passage node as a Space
        door_space = space_repo.create_space(
            SpaceCreate(
                id=door_id,
                display_name=data.display_name,
                space_type=data.space_type,
                campus_id=campus_id,
                floor_id=floor_id,
                centroid_x=cx,
                centroid_y=cy,
                is_accessible=data.is_accessible,
                is_navigable=True,
                traversal_cost=traversal_cost,
            )
        )

        postgis = PostGISService()
        postgis.sync_space(
            build_space_sync_payload(door_space, CampusRepository(db))
        )

        # Create 4 bare CONNECTS_TO edges (A→Door, Door→A, B→Door, Door→B)
        conn_repo.create_connection(data.from_space_id, door_id)
        conn_repo.create_connection(door_id, data.from_space_id)
        conn_repo.create_connection(data.to_space_id, door_id)
        conn_repo.create_connection(door_id, data.to_space_id)

        postgis.sync_connection(
            from_space_id=data.from_space_id,
            to_space_id=data.to_space_id,
            door_space_id=door_id,
            connection_type=data.space_type.value,
            is_accessible=bool(data.is_accessible),
        )

    return Connection(
        from_space_id=data.from_space_id,
        to_space_id=data.to_space_id,
        door_node_id=door_id,
    )


@router.get("/{from_space_id}/{to_space_id}", response_model=Connection)
def get_connection(from_space_id: str, to_space_id: str, db: Database = Depends(get_db)):
    # Find a door node that sits between these two spaces
    result = db.execute(
        """
        MATCH (a:Space {id: $from_id})-[:CONNECTS_TO]->(door:Space)-[:CONNECTS_TO]->(b:Space {id: $to_id})
        WHERE door.space_type IN $conn_types
        RETURN door.id AS door_node_id
        LIMIT 1
        """,
        {
            "from_id": from_space_id,
            "to_id": to_space_id,
            "conn_types": [t.value for t in CONN_SPACE_TYPES],
        },
    )
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No connection from '{from_space_id}' to '{to_space_id}'",
        )
    return Connection(
        from_space_id=from_space_id,
        to_space_id=to_space_id,
        door_node_id=result[0]["door_node_id"],
    )


class ConnectionPatch(BaseModel):
    door_cx: float | None = None
    door_cy: float | None = None
    is_accessible: bool | None = None
    door_type: str | None = None  # STANDARD / AUTOMATIC / LOCKED / EMERGENCY


def _door_space_type_from_str(door_type: str | None) -> SpaceType:
    return {
        "STANDARD": SpaceType.DOOR_STANDARD,
        "AUTOMATIC": SpaceType.DOOR_AUTOMATIC,
        "LOCKED": SpaceType.DOOR_LOCKED,
        "EMERGENCY": SpaceType.DOOR_EMERGENCY,
    }.get((door_type or "").upper(), SpaceType.DOOR_STANDARD)


@router.patch("/{from_space_id}/{to_space_id}", response_model=Connection)
def patch_connection(
    from_space_id: str,
    to_space_id: str,
    data: ConnectionPatch,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Update a Room↔Room connection. Currently supports moving the door
    midpoint (`door_cx`, `door_cy`) and toggling `is_accessible`/`door_type`.

    If a door Space already exists between the two endpoints, its centroid
    is moved. If the connection is currently a direct edge with no door
    Space (e.g. legacy data) and door coordinates are supplied, the direct
    edges are replaced by the door-as-Space pattern."""
    space_repo = SpaceRepository(db)
    conn_repo = ConnectionRepository(db)

    try:
        space_a = space_repo.get_space(from_space_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Space '{from_space_id}' not found")
    try:
        space_b = space_repo.get_space(to_space_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Space '{to_space_id}' not found")

    org_id = space_a.get("organization_id") or space_b.get("organization_id")
    require_org_match(principal, org_id)

    # Look up an existing door Space between these endpoints.
    conn_types = [t.value for t in CONN_SPACE_TYPES]
    door_rows = db.execute(
        """
        MATCH (a:Space {id: $from_id})-[:CONNECTS_TO]->(door:Space)-[:CONNECTS_TO]->(b:Space {id: $to_id})
        WHERE door.space_type IN $conn_types
        RETURN door.id AS id LIMIT 1
        """,
        {"from_id": from_space_id, "to_id": to_space_id, "conn_types": conn_types},
    )
    door_id = door_rows[0]["id"] if door_rows else None

    with audit_action("patch_connection", principal, organization_id=org_id) as detail:
        detail["from_space_id"] = from_space_id
        detail["to_space_id"] = to_space_id

        if door_id:
            # Recompute global lat/lng from the building's origin if available.
            building_id = space_a.get("building_id") or space_b.get("building_id")
            global_lat, global_lng = None, None
            if building_id and data.door_cx is not None and data.door_cy is not None:
                try:
                    building = CampusRepository(db).get_building(building_id)
                    if building.get("origin_lat") is not None and building.get("origin_lng") is not None:
                        global_lat, global_lng = local_to_global_coordinates(
                            data.door_cx, data.door_cy,
                            building["origin_lat"], building["origin_lng"],
                            building.get("origin_bearing") or 0.0,
                        )
                except Exception:
                    pass

            update = SpaceUpdate(
                centroid_x=data.door_cx,
                centroid_y=data.door_cy,
                centroid_lat=global_lat,
                centroid_lng=global_lng,
                is_accessible=data.is_accessible,
                space_type=(_door_space_type_from_str(data.door_type)
                            if data.door_type is not None else None),
            )
            try:
                door_space = space_repo.update_space(door_id, update)
            except Exception:
                raise HTTPException(status_code=404, detail=f"Door space '{door_id}' not found")

            postgis = PostGISService()
            postgis.sync_space(build_space_sync_payload(door_space, CampusRepository(db)))
            if data.is_accessible is not None:
                postgis.update_connection_group_access(door_id, bool(data.is_accessible))

            detail["door_id"] = door_id
            return Connection(
                from_space_id=from_space_id,
                to_space_id=to_space_id,
                door_node_id=door_id,
            )

        # No door Space yet — promote a direct edge to the door-as-Space
        # pattern using the supplied coordinates. Requires door_cx/door_cy.
        if data.door_cx is None or data.door_cy is None:
            raise HTTPException(
                status_code=422,
                detail="No door space exists for this connection; supply door_cx and door_cy to create one.",
            )

        # Drop any existing direct edges between the two endpoints.
        conn_repo.delete_connection(from_space_id, to_space_id)
        conn_repo.delete_connection(to_space_id, from_space_id)

        new_door_id = f"door_{uuid.uuid4().hex[:12]}"
        door_space_type = _door_space_type_from_str(data.door_type)
        floor_a = space_a.get("floor_id")
        floor_b = space_b.get("floor_id")
        floor_id = floor_a if floor_a == floor_b else None
        building_id = space_a.get("building_id")
        if space_b.get("building_id") != building_id:
            building_id = None
        campus_id = space_a.get("campus_id")
        traversal_cost = compute_traversal_cost(door_space_type.value, None, None, None)
        is_accessible = bool(data.is_accessible) if data.is_accessible is not None else True

        door_space = space_repo.create_space(
            SpaceCreate(
                id=new_door_id,
                display_name=door_space_type.value.replace("_", " ").title(),
                space_type=door_space_type,
                campus_id=campus_id,
                building_id=building_id,
                floor_id=floor_id,
                centroid_x=data.door_cx,
                centroid_y=data.door_cy,
                is_accessible=is_accessible,
                is_navigable=True,
                traversal_cost=traversal_cost,
            )
        )

        postgis = PostGISService()
        postgis.sync_space(build_space_sync_payload(door_space, CampusRepository(db)))

        conn_repo.create_connection(from_space_id, new_door_id)
        conn_repo.create_connection(new_door_id, from_space_id)
        conn_repo.create_connection(to_space_id, new_door_id)
        conn_repo.create_connection(new_door_id, to_space_id)

        postgis.sync_connection(
            from_space_id=from_space_id,
            to_space_id=to_space_id,
            door_space_id=new_door_id,
            connection_type=door_space_type.value,
            is_accessible=is_accessible,
        )

        detail["door_id"] = new_door_id
        return Connection(
            from_space_id=from_space_id,
            to_space_id=to_space_id,
            door_node_id=new_door_id,
        )


@router.delete("/{from_space_id}/{to_space_id}", status_code=204)
def delete_connection(
    from_space_id: str,
    to_space_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    space_repo = SpaceRepository(db)
    try:
        endpoint = space_repo.get_space(from_space_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Space '{from_space_id}' not found")
    org_id = endpoint.get("organization_id") if isinstance(endpoint, dict) else None
    require_org_match(principal, org_id)

    with audit_action("delete_connection", principal, organization_id=org_id) as detail:
        detail["from_space_id"] = from_space_id
        detail["to_space_id"] = to_space_id

        conn_types = [t.value for t in CONN_SPACE_TYPES]

        door_rows = db.execute(
            """
            MATCH (a:Space {id: $from_id})-[:CONNECTS_TO]->(door:Space)-[:CONNECTS_TO]->(b:Space {id: $to_id})
            WHERE door.space_type IN $conn_types
            RETURN DISTINCT door.id AS id
            """,
            {"from_id": from_space_id, "to_id": to_space_id, "conn_types": conn_types},
        )
        door_ids = [r["id"] for r in door_rows]

        result = db.execute_write(
            """
            MATCH (a:Space {id: $from_id})-[:CONNECTS_TO]->(door:Space)-[:CONNECTS_TO]->(b:Space {id: $to_id})
            WHERE door.space_type IN $conn_types
            DETACH DELETE door
            RETURN count(door) AS deleted
            """,
            {"from_id": from_space_id, "to_id": to_space_id, "conn_types": conn_types},
        )
        if not result or result[0]["deleted"] == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No connection from '{from_space_id}' to '{to_space_id}'",
            )

        postgis = PostGISService()
        for door_id in door_ids:
            postgis.delete_connection_group(door_id)
            postgis.delete_space(door_id)
