import os

import httpx
from fastapi import FastAPI, Request, Response

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
ASSISTANT_URL = os.getenv("ASSISTANT_URL", "http://assistant:8001")

app = FastAPI(title="Indoor Navigation Gateway", version="1.0.0")


async def _proxy(request: Request, target_base: str) -> Response:
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

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


@app.get("/health")
async def health():
    backend_ok = False
    assistant_ok = False

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

    return {
        "status": "ok" if backend_ok else "degraded",
        "backend": "ok" if backend_ok else "unavailable",
        "assistant": "ok" if assistant_ok else "unavailable",
    }


@app.api_route("/api/v1/assistant/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_assistant(request: Request, path: str):
    return await _proxy(request, ASSISTANT_URL)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_backend(request: Request, path: str):
    return await _proxy(request, BACKEND_URL)
