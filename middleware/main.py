import os
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
ASSISTANT_URL = os.getenv("ASSISTANT_URL", "http://assistant:8001")
IMAGE_PIPELINE_URL = os.getenv("IMAGE_PIPELINE_URL", "http://image_pipeline:8002")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


MIDDLEWARE_DEBUG_UPSTREAMS = _env_flag("MIDDLEWARE_DEBUG_UPSTREAMS")

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

app = FastAPI(
    title="Indoor Navigation Gateway",
    version="1.0.0",
    description=(
        "Gateway for backend, assistant, and image pipeline services. "
        "The docs show middleware-owned routes only."
    ),
    openapi_tags=(
        [{"name": "debug", "description": "Gateway-level debugging and upstream inspection."}]
        if MIDDLEWARE_DEBUG_UPSTREAMS
        else None
    ),
)


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
