import uuid

from fastapi import APIRouter, HTTPException, Depends
from db import Database, get_db
from models.connection import Connection, ConnectionCreate
from models.space import SpaceCreate
from models.enums import CONN_SPACE_TYPES
from repositories.connection_repo import ConnectionRepository
from repositories.space_repo import SpaceRepository
from services.geometry_service import compute_traversal_cost, find_shared_edge_midpoint

router = APIRouter(prefix="/connections", tags=["connections"])


@router.post("", response_model=Connection, status_code=201)
def create_connection(data: ConnectionCreate, db: Database = Depends(get_db)):
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

    # Compute door position: prefer shared edge midpoint, fall back to centroid midpoint
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

    traversal_cost = compute_traversal_cost(data.space_type.value, None, None, None)

    # Inherit context from space A; cross-floor connections get no floor_id
    campus_id = space_a.get("campus_id")
    floor_a = space_a.get("floor_id")
    floor_b = space_b.get("floor_id")
    floor_id = floor_a if floor_a == floor_b else None

    # Create the intermediate door/passage node as a Space
    space_repo.create_space(
        SpaceCreate(
            id=door_id,
            display_name=data.display_name,
            space_type=data.space_type,
            campus_id=campus_id,
            centroid_x=cx,
            centroid_y=cy,
            is_accessible=data.is_accessible,
            is_navigable=True,
            traversal_cost=traversal_cost,
        )
    )

    # Create 4 bare CONNECTS_TO edges (A→Door, Door→A, B→Door, Door→B)
    conn_repo.create_connection(data.from_space_id, door_id)
    conn_repo.create_connection(door_id, data.from_space_id)
    conn_repo.create_connection(data.to_space_id, door_id)
    conn_repo.create_connection(door_id, data.to_space_id)

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


@router.delete("/{from_space_id}/{to_space_id}", status_code=204)
def delete_connection(from_space_id: str, to_space_id: str, db: Database = Depends(get_db)):
    # Find and delete intermediate door nodes between the two spaces
    result = db.execute_write(
        """
        MATCH (a:Space {id: $from_id})-[:CONNECTS_TO]->(door:Space)-[:CONNECTS_TO]->(b:Space {id: $to_id})
        WHERE door.space_type IN $conn_types
        DETACH DELETE door
        RETURN count(door) AS deleted
        """,
        {
            "from_id": from_space_id,
            "to_id": to_space_id,
            "conn_types": [t.value for t in CONN_SPACE_TYPES],
        },
    )
    if not result or result[0]["deleted"] == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No connection from '{from_space_id}' to '{to_space_id}'",
        )
