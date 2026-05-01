"""Thin client for the dedicated email-service container."""

from __future__ import annotations
import logging
import urllib.request
import json
from core.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.email_service_url and settings.internal_email_token)


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """POST to the email service. Returns True on 2xx, False otherwise.
    Network errors are logged but not raised — the caller decides whether
    a failure is fatal."""
    if not is_configured():
        logger.warning("Email service not configured; dropping message to %s", to)
        return False

    payload = {"to": to, "subject": subject, "text": text}
    if html:
        payload["html"] = html

    req = urllib.request.Request(
        f"{settings.email_service_url.rstrip('/')}/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": settings.internal_email_token or "",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        logger.warning("Email send failed for %s: %s", to, exc)
        return False
