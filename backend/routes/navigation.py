from fastapi import APIRouter, HTTPException, Depends, Query
from db import Database, get_db
from core.auth_principal import require_role
from core.exceptions import NavigationError, SpaceNotFound
from models.navigation import Route
from services.navigation_service import NavigationService
from services.gds_service import GdsService

router = APIRouter(prefix="/navigate", tags=["navigation"])


@router.get("", response_model=Route)
def navigate(
    from_space_id: str = Query(..., alias="from"),
    to_space_id: str = Query(..., alias="to"),
    accessible_only: bool = Query(False),
    db: Database = Depends(get_db),
):
    try:
        return NavigationService(db).get_route(from_space_id, to_space_id, accessible_only)
    except SpaceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NavigationError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/refresh-graph", dependencies=[Depends(require_role("editor"))])
def refresh_graph(db: Database = Depends(get_db)):
    """Rebuild the GDS navigation graph projection (call after bulk imports).
    The GDS projection is global (single graph for the whole instance) so we
    require an authenticated editor but no org-match: any editor in any org
    can rebuild it."""
    ok = GdsService(db).refresh_projection()
    return {"success": ok, "message": "GDS projection refreshed" if ok else "GDS not available"}
