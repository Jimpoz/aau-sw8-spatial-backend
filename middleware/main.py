import os
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
ASSISTANT_URL = os.getenv("ASSISTANT_URL", "http://assistant:8001")
IMAGE_PIPELINE_URL = os.getenv("IMAGE_PIPELINE_URL", "http://image_pipeline:8002")
API_SECRET = os.getenv("API_SECRET", "")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


MIDDLEWARE_DEBUG_UPSTREAMS = _env_flag("MIDDLEWARE_DEBUG_UPSTREAMS")


class CampusListItem(BaseModel):
    id: str
    name: str

OPENAPI_SOURCES = (
    {
        "name": "backend",
        "base_url": BACKEND_URL,
        "public_prefixes": ("/api/v1/",),
        "exclude_prefixes": ("/api/v1/assistant/", "/api/v1/room-summary"),
    },
    {
        "name": "assistant",
        "base_url": ASSISTANT_URL,
        "public_prefixes": ("/api/v1/assistant/",),
        "exclude_prefixes": (),
    },
    {
        "name": "image_pipeline",
        "base_url": IMAGE_PIPELINE_URL,
        "public_prefixes": ("/api/v1/room-summary",),
        "exclude_prefixes": (),
    },
)

_UNPROTECTED_PATHS = {"/health"}


app = FastAPI(
    title="Indoor Navigation Gateway",
    version="1.0.0",
    description=(
        "Gateway for backend, assistant, and image pipeline services. "
        "The docs show middleware-owned routes only."
    ),
    openapi_tags=[
        {"name": "mobile", "description": "Lightweight endpoints for mobile clients."},
    ]
    + (
        [{"name": "debug", "description": "Gateway-level debugging and upstream inspection."}]
        if MIDDLEWARE_DEBUG_UPSTREAMS
        else []
    ),
)


@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    if request.url.path in _UNPROTECTED_PATHS:
        return await call_next(request)
    if not API_SECRET or request.headers.get("X-Api-Key") != API_SECRET:
        return Response(
            content=b'{"detail":"Unauthorized"}',
            status_code=401,
            headers={"Content-Type": "application/json"},
        )
    return await call_next(request)


async def _probe_url(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    try:
        response = await client.get(url)
        return {"ok": response.status_code == 200, "status_code": response.status_code}
    except Exception as exc:
        return {"ok": False, "status_code": None, "error": str(exc)}


async def _proxy(request: Request, target_base: str, upstream_name: str) -> Response:
    path = request.url.path
    query = str(request.url.query)
    url = f"{target_base}{path}"
    if query:
        url = f"{url}?{query}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    async with httpx.AsyncClient(timeout=900.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )

    response_headers = dict(resp.headers)
    response_headers["X-Gateway-Upstream"] = upstream_name

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
    )


def _strip_space_images(space: dict) -> None:
    if "room_images" in space:
        space["room_images"] = []
    metadata = space.get("metadata")
    if isinstance(metadata, dict):
        rs = metadata.get("room_summary")
        if isinstance(rs, dict):
            rs["room_images"] = []
            for view in rs.get("views", []):
                if isinstance(view, dict):
                    view.pop("svg", None)
    for subspace in space.get("subspaces", []):
        _strip_space_images(subspace)


def _strip_image_data(data: dict) -> None:
    campus = data.get("campus")
    if not campus:
        return
    for building in campus.get("buildings", []):
        for floor in building.get("floors", []):
            for space in floor.get("spaces", []):
                _strip_space_images(space)


async def debug_upstreams() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        upstreams = []
        for source in OPENAPI_SOURCES:
            health = await _probe_url(client, f"{source['base_url']}/health")
            openapi = await _probe_url(client, f"{source['base_url']}/openapi.json")
            upstreams.append(
                {
                    "name": source["name"],
                    "base_url": source["base_url"],
                    "public_prefixes": list(source["public_prefixes"]),
                    "health": health,
                    "openapi": openapi,
                }
            )

    return {
        "gateway_docs": {
            "docs_url": "/docs",
            "openapi_url": "/openapi.json",
            "health_url": "/health",
        },
        "upstreams": upstreams,
    }


if MIDDLEWARE_DEBUG_UPSTREAMS:
    app.get("/debug/upstreams", tags=["debug"])(debug_upstreams)


@app.get("/health")
async def health():
    backend_ok = False
    assistant_ok = False
    image_pipeline_ok = False

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{BACKEND_URL}/health")
            backend_ok = r.status_code == 200
        except Exception:
            pass
        try:
            r = await client.get(f"{ASSISTANT_URL}/health")
            assistant_ok = r.status_code == 200
        except Exception:
            pass
        try:
            r = await client.get(f"{IMAGE_PIPELINE_URL}/health")
            image_pipeline_ok = r.status_code == 200
        except Exception:
            pass

    return {
        "status": "ok" if backend_ok and assistant_ok and image_pipeline_ok else "degraded",
        "backend": "ok" if backend_ok else "unavailable",
        "assistant": "ok" if assistant_ok else "unavailable",
        "image_pipeline": "ok" if image_pipeline_ok else "unavailable",
    }


@app.get("/api/v1/mobile/campuses", response_model=list[CampusListItem], tags=["mobile"])
async def mobile_campus_list():
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{BACKEND_URL}/api/v1/campuses")
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return Response(
            content=exc.response.content,
            status_code=exc.response.status_code,
            headers={"Content-Type": "application/json"},
        )
    except httpx.HTTPError:
        return Response(
            content=b'{"detail":"backend unavailable"}',
            status_code=502,
            headers={"Content-Type": "application/json"},
        )
    return [{"id": c["id"], "name": c["name"]} for c in resp.json()]


@app.get("/api/v1/mobile/campuses/{campus_id}/map", tags=["mobile"])
async def mobile_map_full(campus_id: str):
    async with httpx.AsyncClient(timeout=900.0) as client:
        resp = await client.get(f"{BACKEND_URL}/api/v1/campuses/{campus_id}/export")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": "application/json", "X-Gateway-Upstream": "backend"},
    )

# discards svg data for much smaller file sizes
@app.get("/api/v1/mobile/campuses/{campus_id}/map/light", tags=["mobile"])
async def mobile_map_light(campus_id: str):
    async with httpx.AsyncClient(timeout=900.0) as client:
        resp = await client.get(f"{BACKEND_URL}/api/v1/campuses/{campus_id}/export")
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": "application/json"},
        )
    data = resp.json()
    _strip_image_data(data)
    return data


@app.api_route("/api/v1/assistant", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
@app.api_route("/api/v1/assistant/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy_assistant(request: Request, path: str):
    return await _proxy(request, ASSISTANT_URL, "assistant")


@app.api_route("/api/v1/room-summary", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
@app.api_route("/api/v1/room-summary/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy_image_pipeline(request: Request, path: str = ""):
    return await _proxy(request, IMAGE_PIPELINE_URL, "image_pipeline")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy_backend(request: Request, path: str):
    return await _proxy(request, BACKEND_URL, "backend")
