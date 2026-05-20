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


@router.post("", response_model=Landmark, status_code=201)
async def create_landmark(
    name: str = Form(..., min_length=1, max_length=120),
    space_id: str = Form(...),
    image: UploadFile = File(...),
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

    with audit_action(
        "create_landmark", principal, organization_id=org_id
    ) as detail:
        detail["landmark_id"] = landmark_id
        detail["space_id"] = space_id
        detail["name"] = name
        detail["image_bytes"] = len(image_bytes)
        record = LandmarkRepository(db).create_landmark(
            landmark_id=landmark_id,
            name=name,
            space_id=space_id,
            image_b64=image_b64,
            image_width=width,
            image_height=height,
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
