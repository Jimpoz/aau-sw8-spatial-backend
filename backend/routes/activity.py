from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.auth_principal import Principal, require_user
from services.postgis_service import PostGISService

router = APIRouter(prefix="/me/activity", tags=["activity"])


class ActivityTotals(BaseModel):
    day: str
    distance_m: float = 0.0
    steps: int = 0


class ActivityIncrement(BaseModel):
    day: str | None = None
    distance_m: float = Field(default=0.0, ge=0)
    steps: int = Field(default=0, ge=0)


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@router.get("", response_model=ActivityTotals)
def get_activity(
    day: str | None = Query(None, description="Local date YYYY-MM-DD; defaults to today (UTC)."),
    principal: Principal = Depends(require_user),
):
    resolved_day = day or _utc_today()
    if principal.user_id is None:
        return ActivityTotals(day=resolved_day)
    totals = PostGISService().get_daily_activity(principal.user_id, resolved_day)
    return ActivityTotals(day=resolved_day, **totals)


@router.post("", response_model=ActivityTotals)
def add_activity(
    payload: ActivityIncrement,
    principal: Principal = Depends(require_user),
):
    resolved_day = payload.day or _utc_today()
    if principal.user_id is None:
        return ActivityTotals(day=resolved_day)
    totals = PostGISService().add_daily_activity(
        principal.user_id, resolved_day, payload.distance_m, payload.steps
    )
    return ActivityTotals(day=resolved_day, **totals)
