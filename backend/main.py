from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.exceptions import NotFoundError, NavigationError, MapImportError
from core.request_context import (
    current_is_service,
    current_org_id,
    current_user_id,
    current_user_role,
)
from db import get_db
from models.enums import SpaceType, CONN_SPACE_TYPES
from core.config import settings
from routes import (
    organizations,
    campuses,
    buildings,
    floors,
    spaces,
    connections,
    navigation,
    search,
    auth,
)
from scripts.init_db import apply_schema
from services.postgis_service import PostGISService


def _check_jwt_secret_strength() -> None:
    secret = settings.auth_jwt_secret
    if not secret:
        return
    if len(secret) < 32:
        raise RuntimeError(
            "AUTH_JWT_SECRET must be at least 32 characters. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_jwt_secret_strength()
    db = get_db()
    apply_schema(db)
    if settings.auth_rls_enabled:
        PostGISService().apply_rls_policies()
    yield
    db.close()


app = FastAPI(
    title="Indoor Navigation API",
    version="1.0.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def _populate_request_context(request: Request, call_next):
    user_id = request.headers.get("x-user-id")
    org_id = request.headers.get("x-org-id")
    role = request.headers.get("x-user-role")

    user_token = current_user_id.set(user_id)
    org_token = current_org_id.set(org_id)
    role_token = current_user_role.set(role)
    service_token = current_is_service.set(user_id is None)
    try:
        return await call_next(request)
    finally:
        current_user_id.reset(user_token)
        current_org_id.reset(org_token)
        current_user_role.reset(role_token)
        current_is_service.reset(service_token)


# --- Exception handlers ---

@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(NavigationError)
async def nav_error_handler(request: Request, exc: NavigationError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(MapImportError)
async def import_error_handler(request: Request, exc: MapImportError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# --- Routers ---

PREFIX = "/api/v1"

app.include_router(organizations.router, prefix=PREFIX)
app.include_router(campuses.router, prefix=PREFIX)
app.include_router(buildings.router, prefix=PREFIX)
app.include_router(floors.router, prefix=PREFIX)
app.include_router(spaces.router, prefix=PREFIX)
app.include_router(connections.router, prefix=PREFIX)
app.include_router(navigation.router, prefix=PREFIX)
app.include_router(search.router, prefix=PREFIX)

if settings.auth_jwt_secret:
    app.include_router(auth.router, prefix=PREFIX)


@app.get(f"{PREFIX}/enums/space-types")
def get_space_types():
    return {
        "space_types": [t.value for t in SpaceType],
        "connection_types": [t.value for t in CONN_SPACE_TYPES],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
