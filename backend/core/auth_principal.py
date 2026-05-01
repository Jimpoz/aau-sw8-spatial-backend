from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from fastapi import Depends, HTTPException, Request
from core.config import settings

_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def _enforcement_enabled() -> bool:
    return bool(settings.auth_jwt_secret)


@dataclass(frozen=True)
class Principal:
    """Identity + role + active org of the calling user."""

    user_id: str | None
    org_id: str | None
    role: str | None
    is_mapmaker: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None or self.is_mapmaker

    def has_role(self, min_role: str) -> bool:
        if self.is_mapmaker:
            return True
        if not self.role:
            return False
        try:
            return _ROLE_RANK[self.role] >= _ROLE_RANK[min_role]
        except KeyError:
            return False


def get_principal(request: Request) -> Principal:
    if request.headers.get("x-map-maker") == "1":
        return Principal(
            user_id=None,
            org_id=None,
            role="owner",
            is_mapmaker=True,
        )
    return Principal(
        user_id=request.headers.get("x-user-id"),
        org_id=request.headers.get("x-org-id"),
        role=request.headers.get("x-user-role"),
    )


def require_user(principal: Principal = Depends(get_principal)) -> Principal:
    if not _enforcement_enabled() or principal.is_mapmaker:
        return principal
    if not principal.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def require_role(min_role: str) -> Callable[[Principal], Principal]:
    if min_role not in _ROLE_RANK:
        raise ValueError(f"Unknown role {min_role!r}; expected one of {sorted(_ROLE_RANK)}")

    def _dep(principal: Principal = Depends(require_user)) -> Principal:
        if not _enforcement_enabled() or principal.is_mapmaker:
            return principal
        if not principal.has_role(min_role):
            raise HTTPException(
                status_code=403,
                detail=f"Requires role '{min_role}' or higher",
            )
        return principal

    return _dep


def require_org_match(principal: Principal, organization_id: str | None) -> None:
    if not _enforcement_enabled() or principal.is_mapmaker:
        return
    if organization_id is None:
        return
    if principal.org_id != organization_id:
        raise HTTPException(
            status_code=403,
            detail="Resource belongs to a different organization",
        )
