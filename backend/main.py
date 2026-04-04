from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.exceptions import NotFoundError, NavigationError, MapImportError
from db import get_db
from models.enums import SpaceType, CONN_SPACE_TYPES
from routes import campuses, buildings, floors, spaces, connections, navigation, search
from scripts.init_db import apply_schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    apply_schema(db)
    yield
    db.close()


app = FastAPI(
    title="Indoor Navigation API",
    version="1.0.0",
    lifespan=lifespan,
)

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

app.include_router(campuses.router, prefix=PREFIX)
app.include_router(buildings.router, prefix=PREFIX)
app.include_router(floors.router, prefix=PREFIX)
app.include_router(spaces.router, prefix=PREFIX)
app.include_router(connections.router, prefix=PREFIX)
app.include_router(navigation.router, prefix=PREFIX)
app.include_router(search.router, prefix=PREFIX)


@app.get(f"{PREFIX}/enums/space-types")
def get_space_types():
    return {
        "space_types": [t.value for t in SpaceType],
        "connection_types": [t.value for t in CONN_SPACE_TYPES],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
