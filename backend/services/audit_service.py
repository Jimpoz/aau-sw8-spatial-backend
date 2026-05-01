"""Append-only audit log writes for non-auth code paths."""

from __future__ import annotations
import uuid
from contextlib import contextmanager
from fastapi import HTTPException
from core.auth_principal import Principal
from services.postgis_service import AuditLog, PostGISService

_4XX_DO_NOT_AUDIT = frozenset({404, 422})


def write_audit_log(
    action: str,
    success: bool,
    subject_user_id: str | None = None,
    subject_email: str | None = None,
    organization_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> bool:
    """Insert one audit_log row in its own short transaction."""
    pg = PostGISService()
    if not pg.engine or not pg.SessionLocal:
        return False
    try:
        session = pg.SessionLocal()
        try:
            session.add(AuditLog(
                id=str(uuid.uuid4()),
                subject_user_id=subject_user_id,
                subject_email=subject_email,
                organization_id=organization_id,
                action=action,
                success=success,
                ip_address=ip_address,
                user_agent=user_agent,
                detail=detail,
            ))
            session.commit()
            return True
        finally:
            session.close()
    except Exception as exc:
        print(f"[audit] Failed to write audit row for action={action!r}: {exc}")
        return False


@contextmanager
def audit_action(
    action: str,
    principal: Principal | None,
    *,
    organization_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
):
    """Wrap a mutating route body so it always emits exactly one audit row."""
    detail: dict = {}
    subject_user_id = principal.user_id if principal else None
    try:
        yield detail
    except HTTPException as exc:
        if exc.status_code not in _4XX_DO_NOT_AUDIT:
            write_audit_log(
                action=action,
                success=False,
                subject_user_id=subject_user_id,
                organization_id=organization_id,
                ip_address=ip_address,
                user_agent=user_agent,
                detail={**detail, "error": str(exc.detail), "status": exc.status_code},
            )
        raise
    except Exception as exc:
        write_audit_log(
            action=action,
            success=False,
            subject_user_id=subject_user_id,
            organization_id=organization_id,
            ip_address=ip_address,
            user_agent=user_agent,
            detail={**detail, "error": str(exc)},
        )
        raise
    write_audit_log(
        action=action,
        success=True,
        subject_user_id=subject_user_id,
        organization_id=organization_id,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=detail,
    )
