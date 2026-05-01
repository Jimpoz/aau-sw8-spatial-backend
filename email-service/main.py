"""Email service.

Three transports:
  1. **Dummy / dry-run** (default) — ``SMTP_HOST`` unset. The service
     prints the full email body (incl. OTP) to stdout, optionally writes
     a copy to ``MAILBOX_DIR``, and returns 200. No external dependency,
     no account setup. View OTPs with ``docker compose logs email`` or
     read the .eml files. Best for local dev / CI.
  2. **STARTTLS** — typically port 587 (Gmail, Brevo, SendGrid). Plain
     SMTP socket upgraded to TLS via STARTTLS.
  3. **Implicit SSL (SMTPS)** — typically port 465. TLS from the first
     byte; auto-detected on port 465 unless ``SMTP_USE_SSL`` overrides.
"""

from __future__ import annotations

import hmac
import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger("email-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

app = FastAPI(title="Ariadne Email Service", version="1.0.0")


# --- Config (env-driven) ---

def _env(name: str) -> str | None:
    """Empty string from compose's ``${VAR:-}`` should be treated as unset
    so default-driven branches (e.g. SSL auto-detect on port 465) work."""
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    return val


INTERNAL_TOKEN = os.environ.get("INTERNAL_EMAIL_TOKEN", "")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT") or "587")
SMTP_USERNAME = _env("SMTP_USERNAME")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_USE_TLS = (_env("SMTP_USE_TLS") or "true").lower() in ("1", "true", "yes")
_SMTP_USE_SSL_ENV = _env("SMTP_USE_SSL")
if _SMTP_USE_SSL_ENV is None:
    SMTP_USE_SSL = SMTP_PORT == 465
else:
    SMTP_USE_SSL = _SMTP_USE_SSL_ENV.lower() in ("1", "true", "yes")
SMTP_FROM_ADDRESS = _env("SMTP_FROM_ADDRESS") or "no-reply@ariadne.local"
SMTP_FROM_NAME = _env("SMTP_FROM_NAME") or "Ariadne"
MAILBOX_DIR = _env("MAILBOX_DIR")
DRY_RUN = SMTP_HOST is None

if MAILBOX_DIR:
    Path(MAILBOX_DIR).mkdir(parents=True, exist_ok=True)


# --- Models ---

class SendRequest(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)
    html: str | None = Field(default=None, max_length=50_000)


class SendResponse(BaseModel):
    success: bool
    dry_run: bool


# --- Auth ---

def _check_token(token: str | None) -> None:
    """Constant-time compare to mitigate timing oracles. We also fail
    closed when ``INTERNAL_EMAIL_TOKEN`` is unset to prevent an
    accidentally-deployed open relay."""
    if not INTERNAL_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Email service not configured (INTERNAL_EMAIL_TOKEN unset)",
        )
    if not token or not hmac.compare_digest(token, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid internal token")


# --- Routes ---

@app.get("/health")
def health():
    return {
        "status": "ok",
        "dry_run": DRY_RUN,
        "smtp_host": SMTP_HOST,
        "smtp_port": SMTP_PORT,
        "tls": "ssl" if SMTP_USE_SSL else ("starttls" if SMTP_USE_TLS else "none"),
    }


@app.post("/send", response_model=SendResponse)
def send_email(
    payload: SendRequest,
    x_internal_token: str | None = Header(default=None),
):
    _check_token(x_internal_token)

    msg = EmailMessage()
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_ADDRESS}>"
    msg["To"] = payload.to
    msg["Subject"] = payload.subject
    msg.set_content(payload.text)
    if payload.html:
        msg.add_alternative(payload.html, subtype="html")

    if DRY_RUN:
        # Dummy mode: print the full email so the OTP is visible in
        # ``docker compose logs email``. Bordered for readability when
        # interleaved with other services' logs.
        border = "=" * 72
        logger.info(
            "\n%s\n[DUMMY EMAIL] to=%s\nSubject: %s\n%s\n%s\n%s",
            border, payload.to, payload.subject, "-" * 72, payload.text, border,
        )
        if MAILBOX_DIR:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            safe_to = payload.to.replace("@", "_at_").replace("/", "_")
            path = Path(MAILBOX_DIR) / f"{ts}_{safe_to}.eml"
            try:
                path.write_bytes(bytes(msg))
                logger.info("[DUMMY EMAIL] saved copy at %s", path)
            except OSError as exc:
                logger.warning("[DUMMY EMAIL] could not write %s: %s", path, exc)
        return SendResponse(success=True, dry_run=True)

    try:
        if SMTP_USE_SSL:
            # Implicit SSL/TLS handshake on connect (SMTPS, port 465).
            # Do NOT call starttls() afterwards — the connection is already
            # encrypted and Gmail / others will reject a second handshake.
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=15) as smtp:
                if SMTP_USERNAME and SMTP_PASSWORD:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(msg)
        elif SMTP_USE_TLS:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.ehlo()
                if SMTP_USERNAME and SMTP_PASSWORD:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                if SMTP_USERNAME and SMTP_PASSWORD:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(msg)
    except Exception as exc:
        logger.exception("SMTP send failed")
        raise HTTPException(status_code=502, detail=f"SMTP send failed: {exc}")

    return SendResponse(success=True, dry_run=False)
