from __future__ import annotations

import base64
import io
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from core.auth_principal import Principal, require_org_match, require_role
from core.exceptions import SpaceNotFound
from db import Database, get_db
from models.landmark import Landmark
from repositories.landmark_repo import LandmarkRepository
from repositories.space_repo import SpaceRepository
from services.audit_service import audit_action
from services.geometry_service import (
    global_to_local_coordinates,
    local_to_global_coordinates,
)
from services.postgis_service import PostGISService

router = APIRouter(prefix="/landmarks", tags=["landmarks"])


_MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB — generous for a phone JPEG
_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "application/octet-stream"}


def _image_dimensions(image_bytes: bytes) -> tuple[Optional[int], Optional[int]]:
    """Best-effort width/height extraction via PIL."""
    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.width, img.height
    except Exception:
        return None, None


def _space_georef(db: Database, space: dict) -> Optional[dict]:
    """Floor georef for a Space."""
    floor_id = space.get("floor_id") if isinstance(space, dict) else None
    building_id = space.get("building_id") if isinstance(space, dict) else None
    if not floor_id and not building_id:
        return None
    rows = db.execute(
        """
        OPTIONAL MATCH (f:Floor {id: $floor_id})
        OPTIONAL MATCH (b:Building {id: $building_id})
        RETURN
          coalesce(f.origin_lat, b.origin_lat)         AS lat,
          coalesce(f.origin_lng, b.origin_lng)         AS lng,
          coalesce(f.origin_bearing, b.origin_bearing) AS bearing,
          coalesce(f.scale_factor, b.scale_factor)     AS scale
        """,
        {"floor_id": floor_id, "building_id": building_id},
    )
    if not rows:
        return None
    r = rows[0]
    if r["lat"] is None or r["lng"] is None:
        return None
    return {
        "origin_lat": float(r["lat"]),
        "origin_lng": float(r["lng"]),
        "bearing": float(r["bearing"] or 0.0),
        "scale": float(r["scale"] or 1.0),
    }


def _resolve_landmark_coords(
    *,
    space: dict,
    cx: Optional[float],
    cy: Optional[float],
    c_lat: Optional[float],
    c_lng: Optional[float],
    db: Optional[Database] = None,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Cross-fill landmark coordinates between local-floor (x, y) and
    WGS84 (lat, lng). Returns (cx, cy, c_lat, c_lng); each pair is either
    both populated or both None when no georef is available."""
    have_local = cx is not None and cy is not None
    have_global = c_lat is not None and c_lng is not None
    if have_local == have_global:
        return cx, cy, c_lat, c_lng
    if db is None:
        return cx, cy, c_lat, c_lng
    georef = _space_georef(db, space)
    if georef is None:
        return cx, cy, c_lat, c_lng
    if have_local:
        lat, lng = local_to_global_coordinates(
            float(cx), float(cy),
            georef["origin_lat"], georef["origin_lng"],
            georef["bearing"], georef["scale"],
        )
        return cx, cy, lat, lng
    # have_global
    lx, ly = global_to_local_coordinates(
        float(c_lat), float(c_lng),
        georef["origin_lat"], georef["origin_lng"],
        georef["bearing"],
    )
    return lx, ly, c_lat, c_lng


@router.post("", response_model=Landmark, status_code=201)
async def create_landmark(
    name: str = Form(..., min_length=1, max_length=120),
    space_id: str = Form(...),
    image: UploadFile = File(...),
    centroid_x: Optional[float] = Form(None),
    centroid_y: Optional[float] = Form(None),
    centroid_lat: Optional[float] = Form(None),
    centroid_lng: Optional[float] = Form(None),
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Register a new visual landmark for a Space."""

    try:
        space = SpaceRepository(db).get_space(space_id)
    except SpaceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    org_id = space.get("organization_id") if isinstance(space, dict) else None
    require_org_match(principal, org_id)

    if image.content_type and image.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image content-type {image.content_type!r}",
        )

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image too large ({len(image_bytes)} bytes; max {_MAX_IMAGE_BYTES})",
        )

    width, height = _image_dimensions(image_bytes)
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    landmark_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    cx, cy, c_lat, c_lng = _resolve_landmark_coords(
        space=space,
        cx=centroid_x,
        cy=centroid_y,
        c_lat=centroid_lat,
        c_lng=centroid_lng,
        db=db,
    )

    with audit_action(
        "create_landmark", principal, organization_id=org_id
    ) as detail:
        detail["landmark_id"] = landmark_id
        detail["space_id"] = space_id
        detail["name"] = name
        detail["image_bytes"] = len(image_bytes)
        detail["centroid_x"] = cx
        detail["centroid_y"] = cy
        record = LandmarkRepository(db).create_landmark(
            landmark_id=landmark_id,
            name=name,
            space_id=space_id,
            image_b64=image_b64,
            image_width=width,
            image_height=height,
            centroid_x=cx,
            centroid_y=cy,
            centroid_lat=c_lat,
            centroid_lng=c_lng,
            created_by=principal.user_id,
            created_at=created_at,
        )

        sync_payload = dict(record)
        sync_payload["image_b64"] = image_b64
        sync_payload.setdefault("organization_id", org_id)
        PostGISService().sync_landmark(sync_payload)
    return Landmark(**record)


@router.get("", response_model=List[Landmark])
def list_landmarks(
    space_id: Optional[str] = Query(None, description="Filter to a single space"),
    building_id: Optional[str] = Query(None, description="Filter to a whole building"),
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("viewer")),
):
    """List landmarks visible to the caller, optionally filtered."""
    if space_id and building_id:
        raise HTTPException(
            status_code=400,
            detail="Use either space_id or building_id, not both",
        )
    repo = LandmarkRepository(db)
    if space_id:
        rows = repo.list_for_space(space_id)
    elif building_id:
        rows = repo.list_for_building(building_id)
    else:
        raise HTTPException(
            status_code=400,
            detail="Must supply either space_id or building_id",
        )
    return [Landmark(**r) for r in rows]


@router.delete("/{landmark_id}", status_code=204)
def delete_landmark(
    landmark_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    repo = LandmarkRepository(db)
    existing = repo.get_landmark(landmark_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Landmark not found: {landmark_id}")

    space_id = existing.get("space_id")
    if space_id:
        try:
            space = SpaceRepository(db).get_space(space_id)
        except SpaceNotFound:
            space = {}
        require_org_match(principal, space.get("organization_id"))

    with audit_action(
        "delete_landmark", principal,
        organization_id=existing.get("organization_id"),
    ) as detail:
        detail["landmark_id"] = landmark_id
        detail["space_id"] = space_id
        repo.delete_landmark(landmark_id)
        PostGISService().delete_landmark(landmark_id)
