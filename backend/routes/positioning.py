from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth_principal import Principal, require_org_match, require_role
from core.exceptions import SpaceNotFound
from db import Database, get_db
from models.positioning import (
    AccessPointIn,
    FingerprintIn,
    FingerprintOut,
    FloorSurveyResponse,
    LocateRequest,
    LocateResponse,
)
from repositories.space_repo import SpaceRepository
from services.audit_service import audit_action
from services.postgis_service import PostGISService
from services.wifi_positioning import locate_by_rssi, trilaterate_rtt

router = APIRouter(prefix="/positioning", tags=["positioning"])


@router.post("/fingerprints", response_model=FingerprintOut, status_code=201)
def create_fingerprint(
    payload: FingerprintIn,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Store a Wi-Fi fingerprint sample captured inside a space."""
    if not payload.readings:
        raise HTTPException(status_code=400, detail="readings must not be empty")

    try:
        space = SpaceRepository(db).get_space(payload.space_id)
    except SpaceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    org_id = space.get("organization_id") if isinstance(space, dict) else None
    require_org_match(principal, org_id)

    floor_id = payload.floor_id or (space.get("floor_id") if isinstance(space, dict) else None)
    building_id = space.get("building_id") if isinstance(space, dict) else None
    campus_id = space.get("campus_id") if isinstance(space, dict) else None

    fp_id = str(uuid.uuid4())
    with audit_action(
        "create_wifi_fingerprint", principal, organization_id=org_id
    ) as detail:
        detail["space_id"] = payload.space_id
        detail["floor_id"] = floor_id
        detail["ap_count"] = len(payload.readings)
        ok = PostGISService().insert_wifi_fingerprint(
            fp_id=fp_id,
            space_id=payload.space_id,
            floor_id=floor_id,
            building_id=building_id,
            campus_id=campus_id,
            organization_id=org_id,
            readings=payload.readings,
            rtt_distances_mm=payload.rtt_distances_mm,
            sample_count=payload.sample_count,
            created_by=principal.user_id,
        )
    if not ok:
        raise HTTPException(status_code=503, detail="Positioning store unavailable")

    return FingerprintOut(
        id=fp_id,
        space_id=payload.space_id,
        floor_id=floor_id,
        sample_count=payload.sample_count,
    )


@router.get("/fingerprints", response_model=FloorSurveyResponse)
def floor_survey(
    floor_id: str = Query(..., description="Floor to summarise survey coverage for"),
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("viewer")),
):
    """Survey coverage for a floor: how many fingerprints per space."""
    counts = PostGISService().wifi_fingerprint_counts_for_floor(floor_id)
    return FloorSurveyResponse(
        floor_id=floor_id,
        total_fingerprints=sum(counts.values()),
        per_space_counts=counts,
    )


@router.post("/locate", response_model=LocateResponse)
def locate(
    payload: LocateRequest,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("viewer")),
):
    """Resolve the caller's current space from a live Wi-Fi scan (kNN),
    with optional RTT trilateration when AP coordinates are known."""
    if not payload.readings:
        raise HTTPException(status_code=400, detail="readings must not be empty")

    svc = PostGISService()
    fingerprints = svc.wifi_fingerprints_for_floor(payload.floor_id)
    result = locate_by_rssi(payload.readings, fingerprints, k=3)
    if result is None:
        return LocateResponse(method="rssi_knn")

    space_id, confidence, support = result
    resp = LocateResponse(
        space_id=space_id,
        confidence=confidence,
        supporting_count=support,
        method="rssi_knn",
    )

    # RTT refinement (dormant until AP coordinates are surveyed).
    if payload.rtt_distances_mm:
        aps = svc.wifi_access_points_for_floor(payload.floor_id)
        positioned = {
            a["bssid"]: (a["x"], a["y"])
            for a in aps
            if a.get("x") is not None and a.get("y") is not None
        }
        xy = trilaterate_rtt(payload.rtt_distances_mm, positioned)
        if xy is not None:
            resp.x, resp.y = xy
            resp.method = "rssi_knn+rtt"

    return resp


@router.post("/access-points", status_code=204)
def upsert_access_point(
    payload: AccessPointIn,
    db: Database = Depends(get_db),
    principal: Principal = Depends(require_role("editor")),
):
    """Declare/position a known access point (x,y reserved for RTT)."""
    ok = PostGISService().upsert_wifi_access_point(
        bssid=payload.bssid,
        ssid=payload.ssid,
        floor_id=payload.floor_id,
        building_id=None,
        campus_id=None,
        organization_id=principal.org_id,
        x=payload.x,
        y=payload.y,
        supports_rtt=payload.supports_rtt,
    )
    if not ok:
        raise HTTPException(status_code=503, detail="Positioning store unavailable")
