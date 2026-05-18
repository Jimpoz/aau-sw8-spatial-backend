from fastapi import APIRouter, HTTPException, Depends, Query
from db import Database, get_db
from core.auth_principal import require_role
from core.exceptions import NavigationError, SpaceNotFound
from models.navigation import Route
from services.navigation_service import NavigationService
from services.gds_service import GdsService
from services import python_dijkstra

router = APIRouter(prefix="/navigate", tags=["navigation"])


@router.get("", response_model=Route)
def navigate(
    from_space_id: str = Query(..., alias="from"),
    to_space_id: str = Query(..., alias="to"),
    accessible_only: bool = Query(False),
    avoid_stairs: bool = Query(False),
    elevators_only: bool = Query(False),
    db: Database = Depends(get_db),
):
    try:
        return NavigationService(db).get_route(
            from_space_id,
            to_space_id,
            accessible_only=accessible_only,
            avoid_stairs=avoid_stairs,
            elevators_only=elevators_only,
        )
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NavigationError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/refresh-graph", dependencies=[Depends(require_role("editor"))])
def refresh_graph(db: Database = Depends(get_db)):
    """Rebuild the GDS navigation graph projection (call after bulk imports)."""
    ok = GdsService(db).refresh_projection()
    return {"success": ok, "message": "GDS projection refreshed" if ok else "GDS not available"}


def _resolve_space_id(db: Database, value: str) -> tuple[str | None, str | None]:
    """Resolve ``value`` to a Space id. If it already matches a Space.id,
    return that. """
    rows = db.execute(
        "MATCH (s:Space {id: $id}) RETURN s.id AS id, s.display_name AS name",
        {"id": value},
    )
    if rows:
        return rows[0]["id"], rows[0]["name"]
    rows = db.execute(
        """
        MATCH (s:Space)
        WHERE toLower(coalesce(s.display_name, '')) CONTAINS toLower($q)
        RETURN s.id AS id, s.display_name AS name
        ORDER BY size(coalesce(s.display_name, '')) ASC, s.display_name ASC
        LIMIT 1
        """,
        {"q": value},
    )
    if rows:
        return rows[0]["id"], rows[0]["name"]
    return None, None


@router.get("/debug")
def debug_route(
    from_space_id: str = Query(..., alias="from"),
    to_space_id: str = Query(..., alias="to"),
    db: Database = Depends(get_db),
):
    from_id, from_name = _resolve_space_id(db, from_space_id)
    to_id, to_name = _resolve_space_id(db, to_space_id)
    out: dict = {
        "from": {"query": from_space_id, "resolved_id": from_id, "resolved_name": from_name},
        "to":   {"query": to_space_id,   "resolved_id": to_id,   "resolved_name": to_name},
        "gds": {},
        "python": {},
        "native": {},
    }
    if not from_id or not to_id:
        out["error"] = (
            f"could not resolve "
            f"{'from' if not from_id else ''}{' and ' if not from_id and not to_id else ''}"
            f"{'to' if not to_id else ''} — no Space with that id or "
            f"display_name substring exists"
        )
        return out
    from_space_id, to_space_id = from_id, to_id

    # GDS weighted path
    try:
        rows = db.execute(
            """
            MATCH (start:Space {id: $from_id}), (end:Space {id: $to_id})
            CALL gds.shortestPath.dijkstra.stream('navigation-graph', {
                sourceNode: start,
                targetNode: end,
                relationshipWeightProperty: 'weight'
            })
            YIELD path, totalCost, costs
            RETURN
                [n IN nodes(path) | {
                    id: n.id,
                    name: coalesce(n.display_name, n.id),
                    type: n.space_type,
                    cx: n.centroid_x,
                    cy: n.centroid_y,
                    floor: n.floor_index
                }] AS nodes,
                costs,
                totalCost
            """,
            {"from_id": from_space_id, "to_id": to_space_id},
        )
        if rows:
            r = rows[0]
            nodes = r["nodes"]
            costs = r["costs"] or []
            per_hop = []
            for i, n in enumerate(nodes):
                per_hop.append({
                    "step": i,
                    "id": n["id"],
                    "name": n["name"],
                    "type": n["type"],
                    "cumulative_cost_s": float(costs[i]) if i < len(costs) else None,
                })
            out["gds"] = {
                "total_cost_s": r["totalCost"],
                "hops": len(nodes),
                "path": per_hop,
            }
        else:
            out["gds"] = {"error": "no path returned"}
    except Exception as exc:
        out["gds"] = {"error": str(exc)}

    try:
        py = python_dijkstra.find_path(db, from_space_id, to_space_id)
        if py is not None:
            out["python"] = {
                "total_cost_s": py["total_cost"],
                "hops": len(py["path_nodes"]),
                "path": [
                    {
                        "step": i,
                        "id": n["id"],
                        "name": n.get("display_name") or n["id"],
                        "type": n.get("space_type"),
                    }
                    for i, n in enumerate(py["path_nodes"])
                ],
            }
        else:
            out["python"] = {"error": "no path"}
    except Exception as exc:
        out["python"] = {"error": str(exc)}

    try:
        rows = db.execute(
            """
            MATCH (start:Space {id: $from_id}), (end:Space {id: $to_id})
            MATCH path = shortestPath((start)-[:CONNECTS_TO*..100]->(end))
            RETURN [n IN nodes(path) | {
                id: n.id,
                name: coalesce(n.display_name, n.id),
                type: n.space_type
            }] AS nodes
            LIMIT 1
            """,
            {"from_id": from_space_id, "to_id": to_space_id},
        )
        if rows and rows[0]["nodes"]:
            out["native"] = {
                "hops": len(rows[0]["nodes"]),
                "path": rows[0]["nodes"],
            }
        else:
            out["native"] = {"error": "no path"}
    except Exception as exc:
        out["native"] = {"error": str(exc)}

    return out
