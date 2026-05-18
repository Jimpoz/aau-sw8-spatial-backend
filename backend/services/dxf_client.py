from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class PipelineError(Exception):
    """Raised when the pipeline service returns a non-2xx response.

    ``status`` mirrors the upstream HTTP code so the calling route can
    propagate it (e.g. 415 for "no DWG converter installed" surfaces to
    the API client unchanged).
    """

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"indoor data pipeline {status}: {detail}")


def _pipeline_url() -> str:
    return os.getenv(
        "INDOOR_PIPELINE_URL", "http://indoor_data_pipeline:6969"
    ).rstrip("/")


async def parse_dxf(
    file_bytes: bytes,
    filename: str,
    *,
    campus_id: str,
    campus_name: str,
    building_id: str,
    building_name: str,
    floor_id: str,
    floor_index: int = 0,
    floor_display_name: str = "Ground",
    organization_id: Optional[str] = None,
    organization_name: Optional[str] = None,
    organization_entity_type: Optional[str] = None,
    organization_description: Optional[str] = None,
    campus_description: Optional[str] = None,
    building_short_name: Optional[str] = None,
    origin_bearing: float = 0.0,
    layer_mapping: Optional[str] = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Upload bytes to the pipeline, return its ``{schema, summary}``.

    Raises :class:`PipelineError` for non-2xx responses so the route
    can map the upstream status onto its own HTTPException.
    """
    files = {"file": (filename or "upload.dxf", file_bytes)}
    form: dict[str, str] = {
        "campus_id": campus_id,
        "campus_name": campus_name,
        "building_id": building_id,
        "building_name": building_name,
        "floor_id": floor_id,
        "floor_index": str(floor_index),
        "floor_display_name": floor_display_name,
        "origin_bearing": str(origin_bearing),
    }
    optional = {
        "organization_id": organization_id,
        "organization_name": organization_name,
        "organization_entity_type": organization_entity_type,
        "organization_description": organization_description,
        "campus_description": campus_description,
        "building_short_name": building_short_name,
        "layer_mapping": layer_mapping,
    }
    for k, v in optional.items():
        if v is not None:
            form[k] = v

    url = f"{_pipeline_url()}/api/dxf/parse"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, files=files, data=form)

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise PipelineError(resp.status_code, str(detail))

    return resp.json()
