"""Authentication service."""

from datetime import datetime, timedelta, timezone
import hmac
import secrets
import uuid
import bcrypt
import jwt
import pyotp
from core.config import settings
from services.email_client import is_configured as email_configured, send_email
from services.postgis_service import (
    AppUser,
    AuditLog,
    Organization,
    OrganizationMember,
    OrgRole,
    PasswordResetToken,
    PostGISService,
)


class AuthError(Exception):
    """Auth failures that map to 4xx HTTP responses. The router translates
    them into HTTPException with the right status code."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(plain: str) -> str:
    if not plain or len(plain) < 8:
        raise AuthError("Password must be at least 8 characters", status_code=400)
    salt = bcrypt.gensalt(rounds=settings.auth_bcrypt_rounds)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_token(user_id: str, organization_id: str | None, role: str | None) -> tuple[str, datetime]:
    if not settings.auth_jwt_secret:
        raise AuthError(
            "Auth is not configured on this server (AUTH_JWT_SECRET unset)",
            status_code=503,
        )
    iat = _now()
    exp = iat + timedelta(seconds=settings.auth_jwt_ttl_seconds)
    payload = {
        "iss": settings.auth_jwt_issuer,
        "sub": user_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "org_id": organization_id,
        "role": role,
        "typ": "access",
    }
    token = jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256")
    return token, exp


def issue_mfa_challenge(
    user_id: str,
    organization_id: str | None,
    *,
    otp_hash: str | None = None,
    method: str = "totp",
) -> tuple[str, datetime]:
    """Mint a short-lived token that proves the user has just passed the password
    step but not yet the MFA step. Only `/auth/login/mfa` accepts it; the access
    JWT issued there has `typ=access`, this one has `typ=mfa-challenge`.

    For email-OTP MFA the bcrypt hash of the freshly-generated code is sealed
    into the token via the ``otp_hash`` claim. The challenge JWT IS signed,
    so the hash can't be tampered with — and putting it in the token (rather
    than a DB row) keeps email-MFA stateless."""
    if not settings.auth_jwt_secret:
        raise AuthError("Auth is not configured on this server", status_code=503)
    iat = _now()
    exp = iat + timedelta(seconds=settings.auth_mfa_challenge_ttl_seconds)
    payload = {
        "iss": settings.auth_jwt_issuer,
        "sub": user_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "org_id": organization_id,
        "typ": "mfa-challenge",
        "method": method,
    }
    if otp_hash:
        payload["otp_hash"] = otp_hash
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256"), exp


def _send_mfa_email_otp(to_email: str, otp: str) -> bool:
    """Email a 6-digit MFA challenge code to ``to_email``. Returns True on
    success. Returns False (and logs) when the email service is not
    configured — callers decide whether that should fail their flow."""
    if not email_configured():
        return False
    ttl_min = settings.auth_mfa_challenge_ttl_seconds // 60
    text_body = (
        "Your Ariadne sign-in code:\n\n"
        f"    {otp}\n\n"
        f"Enter it in the app within {ttl_min} minutes to finish signing in.\n\n"
        "If you didn't try to sign in, change your password — someone else has it."
    )
    return send_email(
        to=to_email,
        subject="Your Ariadne sign-in code",
        text=text_body,
    )


def decode_token(token: str, *, expected_type: str = "access") -> dict:
    if not settings.auth_jwt_secret:
        raise AuthError("Auth is not configured on this server", status_code=503)
    try:
        claims = jwt.decode(
            token,
            settings.auth_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.auth_jwt_issuer,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Token expired", status_code=401)
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Invalid token: {exc}", status_code=401)
    # Reject tokens being used for the wrong purpose (challenge vs access).
    # Tokens issued before the `typ` claim landed default to "access".
    if claims.get("typ", "access") != expected_type:
        raise AuthError("Token has wrong type for this endpoint", status_code=401)
    return claims


_RECOVERY_CODE_COUNT = 10


def _generate_email_otp() -> str:
    """6-digit numeric OTP, zero-padded. Used for password reset and for
    email-based MFA challenges. Generated with secrets.randbelow so the
    distribution is uniform and unpredictable; the value is hashed before
    storage so the DB row alone can't be replayed."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _generate_recovery_codes() -> list[str]:
    """10 plaintext one-time recovery codes. Returned to the user once at
    enrolment; we only persist their bcrypt hashes."""
    return [secrets.token_hex(5) for _ in range(_RECOVERY_CODE_COUNT)]


def _hash_recovery_codes(codes: list[str]) -> list[str]:
    return [
        bcrypt.hashpw(c.encode("utf-8"), bcrypt.gensalt(rounds=settings.auth_bcrypt_rounds)).decode("utf-8")
        for c in codes
    ]


def _verify_totp_code(secret: str, code: str) -> bool:
    """`pyotp.TOTP.verify` already uses constant-time comparison internally,
    but we sanitize the input first (strip whitespace, reject non-digits)
    to avoid leaking timing through input parsing."""
    if not secret or not code:
        return False
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    try:
        return pyotp.TOTP(secret).verify(cleaned, valid_window=1)
    except Exception:
        return False


def _consume_recovery_code(stored_hashes: list[str], plaintext: str) -> tuple[bool, list[str]]:
    """Returns (matched, remaining_hashes). Constant-time across all entries
    so the response time doesn't leak which slot held the match."""
    if not stored_hashes:
        return False, []
    candidate = plaintext.strip().lower().encode("utf-8")
    matched_index = -1
    for i, h in enumerate(stored_hashes):
        try:
            if bcrypt.checkpw(candidate, h.encode("utf-8")):
                matched_index = i
                # Don't break — keep walking so we burn the same time on a miss.
        except (ValueError, TypeError):
            continue
    if matched_index < 0:
        return False, stored_hashes
    return True, [h for i, h in enumerate(stored_hashes) if i != matched_index]


class AuthService:
    """Postgres-backed auth. Sessions are short-lived, JWT-only — no server-side
    session table in Slice 1. To revoke a token before its `exp`, deactivate
    the user (`app_users.is_active = false`); the middleware re-checks this
    on every request."""

    # Per-process sliding-window rate limiter for password attempts. Keyed on
    # the lowercased email so a stuffing attack that cycles through accounts
    # still concentrates pressure on the limiter. Survives across requests
    # because AuthService is constructed per-request but this attribute is
    # class-level. Resets on process restart — that's fine for slice 7; a
    # Redis-backed limiter is the next step if the deployment grows replicas.
    _LOGIN_ATTEMPTS: dict[str, list[float]] = {}
    _LOGIN_WINDOW_SECONDS = 15 * 60

    def __init__(self):
        pg = PostGISService()
        if not pg.engine or not pg.SessionLocal:
            raise AuthError(
                "Postgres is not reachable; auth is unavailable",
                status_code=503,
            )
        self._SessionLocal = pg.SessionLocal

    @classmethod
    def _check_login_rate_limit(cls, email: str) -> None:
        limit = settings.auth_login_rate_limit
        if limit <= 0:
            return
        now = _now().timestamp()
        attempts = cls._LOGIN_ATTEMPTS.get(email, [])
        # Drop entries older than the window; cheap because the list is small.
        attempts = [t for t in attempts if now - t < cls._LOGIN_WINDOW_SECONDS]
        if len(attempts) >= limit:
            cls._LOGIN_ATTEMPTS[email] = attempts
            raise AuthError(
                "Too many login attempts. Try again in a few minutes.",
                status_code=429,
            )
        cls._LOGIN_ATTEMPTS[email] = attempts

    @classmethod
    def _record_login_attempt(cls, email: str) -> None:
        cls._LOGIN_ATTEMPTS.setdefault(email, []).append(_now().timestamp())

    @classmethod
    def _clear_login_attempts(cls, email: str) -> None:
        cls._LOGIN_ATTEMPTS.pop(email, None)

    # --- audit ---

    def _audit(
        self,
        session,
        action: str,
        success: bool,
        subject_user_id: str | None = None,
        subject_email: str | None = None,
        organization_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict | None = None,
    ) -> None:
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

    # --- core operations ---

    def signup(
        self,
        email: str,
        password: str,
        organization_id: str | None,
        full_name: str | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """Create a new user. If `organization_id` is provided the user is
        also given a VIEWER membership in that org. Owners must be promoted
        manually for now — there is no self-serve org creation in Slice 1."""
        email = email.strip().lower()
        if "@" not in email:
            raise AuthError("Invalid email address", status_code=400)

        session = self._SessionLocal()
        try:
            existing = session.query(AppUser).filter_by(email=email).first()
            if existing:
                self._audit(
                    session, action="signup", success=False,
                    subject_email=email, ip_address=ip_address,
                    user_agent=user_agent,
                    detail={"reason": "email_taken"},
                )
                session.commit()
                raise AuthError("That email is already registered", status_code=409)

            org = None
            if organization_id:
                org = session.query(Organization).filter_by(id=organization_id).first()
                if not org:
                    raise AuthError("Unknown organization", status_code=404)

            user = AppUser(
                id=str(uuid.uuid4()),
                email=email,
                password_hash=hash_password(password),
                full_name=full_name,
                is_active=True,
            )
            session.add(user)
            session.flush()

            role_value: str | None = None
            if org:
                membership = OrganizationMember(
                    user_id=user.id,
                    organization_id=org.id,
                    role=OrgRole.VIEWER,
                )
                session.add(membership)
                role_value = OrgRole.VIEWER.value

            self._audit(
                session, action="signup", success=True,
                subject_user_id=user.id, subject_email=email,
                organization_id=org.id if org else None,
                ip_address=ip_address, user_agent=user_agent,
            )

            session.commit()

            token, exp = issue_token(user.id, org.id if org else None, role_value)
            return {
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                },
                "organization_id": org.id if org else None,
                "role": role_value,
                "token": token,
                "token_expires_at": exp.isoformat(),
            }
        finally:
            session.close()

    def login(
        self,
        email: str,
        password: str,
        organization_id: str | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """Verify credentials and return either a fresh access JWT or — if
        the user has MFA enabled — a short-lived challenge token to be
        presented to /auth/login/mfa with a TOTP code.

        If the user belongs to multiple orgs, the caller must specify
        `organization_id`; otherwise the single membership (if any) is
        auto-selected. A user with no memberships gets a token with
        `org_id=null` so they can still call endpoints that don't require a
        tenant scope."""
        email = email.strip().lower()
        # Apply rate limit before we even hit the DB so we don't burn bcrypt
        # cycles on a credential-stuffing attack.
        self._check_login_rate_limit(email)

        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(email=email).first()
            if not user or not user.is_active or not verify_password(password, user.password_hash):
                self._record_login_attempt(email)
                self._audit(
                    session, action="login", success=False,
                    subject_user_id=user.id if user else None,
                    subject_email=email, ip_address=ip_address,
                    user_agent=user_agent,
                    detail={"reason": "bad_credentials"},
                )
                session.commit()
                # Generic message — don't leak whether the email exists.
                raise AuthError("Invalid credentials", status_code=401)

            memberships = session.query(OrganizationMember).filter_by(
                user_id=user.id
            ).all()

            chosen: OrganizationMember | None = None
            if organization_id:
                chosen = next(
                    (m for m in memberships if m.organization_id == organization_id),
                    None,
                )
                if not chosen:
                    self._audit(
                        session, action="login", success=False,
                        subject_user_id=user.id, subject_email=email,
                        organization_id=organization_id,
                        ip_address=ip_address, user_agent=user_agent,
                        detail={"reason": "not_a_member"},
                    )
                    session.commit()
                    # Don't leak membership state either — same generic 401.
                    raise AuthError("Invalid credentials", status_code=401)
            elif len(memberships) == 1:
                chosen = memberships[0]
            elif len(memberships) > 1:
                raise AuthError(
                    "Multiple organizations available; please specify organization_id",
                    status_code=400,
                )

            org_id = chosen.organization_id if chosen else None
            role_value = chosen.role.value if chosen else None

            # Password is good. Clear the rate-limit slate for this email so
            # a legitimate user who fat-fingered earlier doesn't stay locked.
            self._clear_login_attempts(email)

            if user.mfa_enabled:
                method = user.mfa_method or "totp"
                otp_hash: str | None = None
                if method == "email":
                    # Generate a fresh OTP, email it, and seal its hash into
                    # the challenge JWT. Email failures abort the login —
                    # silently issuing a challenge the user can't satisfy
                    # would just hand them a hung sign-in screen.
                    otp = _generate_email_otp()
                    otp_hash = bcrypt.hashpw(
                        otp.encode("utf-8"),
                        bcrypt.gensalt(rounds=settings.auth_bcrypt_rounds),
                    ).decode("utf-8")
                    if not _send_mfa_email_otp(user.email, otp):
                        raise AuthError(
                            "Could not send MFA email; try again later",
                            status_code=503,
                        )
                self._audit(
                    session, action="login_mfa_challenge", success=True,
                    subject_user_id=user.id, subject_email=email,
                    organization_id=org_id,
                    ip_address=ip_address, user_agent=user_agent,
                    detail={"method": method},
                )
                session.commit()
                challenge, exp = issue_mfa_challenge(
                    user.id, org_id, otp_hash=otp_hash, method=method,
                )
                return {
                    "mfa_required": True,
                    "mfa_method": method,
                    "challenge_token": challenge,
                    "challenge_expires_at": exp.isoformat(),
                }

            self._audit(
                session, action="login", success=True,
                subject_user_id=user.id, subject_email=email,
                organization_id=org_id,
                ip_address=ip_address, user_agent=user_agent,
            )
            session.commit()

            token, exp = issue_token(user.id, org_id, role_value)
            return {
                "mfa_required": False,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                },
                "organization_id": org_id,
                "role": role_value,
                "token": token,
                "token_expires_at": exp.isoformat(),
            }
        finally:
            session.close()

    def login_mfa(
        self,
        challenge_token: str,
        code: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """Second step of two-factor login. Verifies the TOTP code (or a
        recovery code) against the user resolved from the challenge token
        and mints the real access JWT."""
        claims = decode_token(challenge_token, expected_type="mfa-challenge")
        user_id = claims.get("sub")
        org_id_claim = claims.get("org_id")
        challenge_method = claims.get("method", "totp")
        otp_hash_claim = claims.get("otp_hash")
        if not user_id:
            raise AuthError("Invalid challenge token", status_code=401)

        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active or not user.mfa_enabled:
                raise AuthError("Invalid credentials", status_code=401)

            ok = False
            used_recovery_code = False
            if challenge_method == "email" and otp_hash_claim:
                # Email-OTP path: the hash lives in the JWT, not the DB. The
                # JWT signature already binds the user; bcrypt-checking the
                # submitted code is the only step needed.
                cleaned = (code or "").strip().replace(" ", "")
                if cleaned and cleaned.isdigit():
                    try:
                        ok = bcrypt.checkpw(
                            cleaned.encode("utf-8"),
                            otp_hash_claim.encode("utf-8"),
                        )
                    except (ValueError, TypeError):
                        ok = False
            else:
                # TOTP path (authenticator app). Falls back to recovery codes
                # for users who have lost their device.
                if not user.mfa_secret:
                    raise AuthError("Invalid credentials", status_code=401)
                ok = _verify_totp_code(user.mfa_secret, code)
                if not ok and user.mfa_recovery_codes:
                    consumed, remaining = _consume_recovery_code(
                        list(user.mfa_recovery_codes), code,
                    )
                    if consumed:
                        user.mfa_recovery_codes = remaining
                        used_recovery_code = True
                        ok = True

            if not ok:
                self._audit(
                    session, action="login_mfa", success=False,
                    subject_user_id=user.id, subject_email=user.email,
                    organization_id=org_id_claim,
                    ip_address=ip_address, user_agent=user_agent,
                    detail={"reason": "bad_code"},
                )
                session.commit()
                raise AuthError("Invalid credentials", status_code=401)

            # Re-resolve role from the org claim. Guards against the user being
            # removed from the org between login step 1 and step 2.
            membership = None
            if org_id_claim:
                membership = session.query(OrganizationMember).filter_by(
                    user_id=user.id, organization_id=org_id_claim,
                ).first()
                if not membership:
                    raise AuthError("Invalid credentials", status_code=401)

            role_value = membership.role.value if membership else None

            self._audit(
                session, action="login_mfa", success=True,
                subject_user_id=user.id, subject_email=user.email,
                organization_id=org_id_claim,
                ip_address=ip_address, user_agent=user_agent,
                detail={"recovery_code_used": used_recovery_code} if used_recovery_code else None,
            )
            session.commit()

            token, exp = issue_token(user.id, org_id_claim, role_value)
            return {
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                },
                "organization_id": org_id_claim,
                "role": role_value,
                "token": token,
                "token_expires_at": exp.isoformat(),
                "recovery_code_used": used_recovery_code,
                "recovery_codes_remaining": len(user.mfa_recovery_codes or []),
            }
        finally:
            session.close()

    # --- MFA enrolment ---

    def setup_mfa(self, user_id: str) -> dict:
        """Begin TOTP-based MFA enrolment. Generates a fresh TOTP secret +
        recovery codes and stores them on the user, but leaves
        `mfa_enabled=false` until the user submits a valid code via
        `confirm_mfa`. Re-running this on an already-enrolled user rotates
        their secret and burns their old recovery codes."""
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active:
                raise AuthError("User not found", status_code=404)

            secret = pyotp.random_base32()
            recovery_codes = _generate_recovery_codes()
            user.mfa_secret = secret
            user.mfa_recovery_codes = _hash_recovery_codes(recovery_codes)
            user.mfa_method = "totp"
            user.mfa_enabled = False  # not enabled until confirmed
            self._audit(
                session, action="mfa_setup", success=True,
                subject_user_id=user.id, subject_email=user.email,
                detail={"method": "totp"},
            )
            session.commit()

            uri = pyotp.TOTP(secret).provisioning_uri(
                name=user.email,
                issuer_name=settings.auth_jwt_issuer,
            )
            return {
                "secret": secret,
                "provisioning_uri": uri,
                "recovery_codes": recovery_codes,  # ONLY returned this once
            }
        finally:
            session.close()

    def setup_mfa_email(self, user_id: str) -> dict:
        """Begin email-based MFA enrolment. Generates a 6-digit OTP, sends
        it to the user's registered email, and returns a short-lived
        ``setup_challenge_token`` that seals the OTP's bcrypt hash. The
        challenge is exchanged for confirmation in ``confirm_mfa_email``.
        We deliberately do NOT mutate ``mfa_enabled`` here — only after a
        valid code has been confirmed."""
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active:
                raise AuthError("User not found", status_code=404)
            if not email_configured():
                raise AuthError(
                    "Email service is not configured on this server",
                    status_code=503,
                )

            otp = _generate_email_otp()
            otp_hash = bcrypt.hashpw(
                otp.encode("utf-8"),
                bcrypt.gensalt(rounds=settings.auth_bcrypt_rounds),
            ).decode("utf-8")
            if not _send_mfa_email_otp(user.email, otp):
                raise AuthError(
                    "Could not send MFA email; try again later",
                    status_code=503,
                )

            # Issue an MFA-challenge JWT (typ=mfa-challenge, method=email)
            # that sealed the OTP hash. confirm_mfa_email reuses
            # decode_token + the same otp_hash check as login_mfa.
            challenge, exp = issue_mfa_challenge(
                user.id, None, otp_hash=otp_hash, method="email",
            )
            self._audit(
                session, action="mfa_setup", success=True,
                subject_user_id=user.id, subject_email=user.email,
                detail={"method": "email"},
            )
            session.commit()
            recovery_codes = _generate_recovery_codes()
            # Email enrolment also issues recovery codes so a user who
            # loses access to their inbox isn't permanently locked out.
            user.mfa_recovery_codes = _hash_recovery_codes(recovery_codes)
            session.commit()
            return {
                "setup_challenge_token": challenge,
                "challenge_expires_at": exp.isoformat(),
                "recovery_codes": recovery_codes,
            }
        finally:
            session.close()

    def confirm_mfa_email(self, user_id: str, challenge_token: str, code: str) -> dict:
        """Verify the email OTP issued by ``setup_mfa_email`` and flip
        ``mfa_enabled=true`` with ``mfa_method='email'``."""
        claims = decode_token(challenge_token, expected_type="mfa-challenge")
        if claims.get("sub") != user_id or claims.get("method") != "email":
            raise AuthError("Invalid challenge", status_code=401)
        otp_hash_claim = claims.get("otp_hash")
        cleaned = (code or "").strip().replace(" ", "")
        if not otp_hash_claim or not cleaned.isdigit():
            raise AuthError("Invalid code", status_code=401)
        try:
            ok = bcrypt.checkpw(
                cleaned.encode("utf-8"),
                otp_hash_claim.encode("utf-8"),
            )
        except (ValueError, TypeError):
            ok = False

        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active:
                raise AuthError("User not found", status_code=404)
            if not ok:
                self._audit(
                    session, action="mfa_confirm", success=False,
                    subject_user_id=user.id, subject_email=user.email,
                    detail={"method": "email", "reason": "bad_code"},
                )
                session.commit()
                raise AuthError("Invalid code", status_code=401)
            user.mfa_method = "email"
            user.mfa_secret = None  # email-MFA needs no per-user secret
            user.mfa_enabled = True
            self._audit(
                session, action="mfa_confirm", success=True,
                subject_user_id=user.id, subject_email=user.email,
                detail={"method": "email"},
            )
            session.commit()
            return {"mfa_enabled": True, "mfa_method": "email"}
        finally:
            session.close()

    def confirm_mfa(self, user_id: str, code: str) -> dict:
        """Complete MFA enrolment by verifying a code generated from the
        secret returned by `setup_mfa`. Flips `mfa_enabled=true`."""
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active or not user.mfa_secret:
                raise AuthError("MFA setup has not been started", status_code=400)
            if not _verify_totp_code(user.mfa_secret, code):
                self._audit(
                    session, action="mfa_confirm", success=False,
                    subject_user_id=user.id, subject_email=user.email,
                    detail={"reason": "bad_code"},
                )
                session.commit()
                raise AuthError("Invalid code", status_code=401)
            user.mfa_enabled = True
            self._audit(
                session, action="mfa_confirm", success=True,
                subject_user_id=user.id, subject_email=user.email,
            )
            session.commit()
            return {"mfa_enabled": True}
        finally:
            session.close()

    def disable_mfa(self, user_id: str, password: str) -> dict:
        """Turn MFA off after re-confirming the user's password. Wipes the
        secret and recovery codes."""
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active:
                raise AuthError("User not found", status_code=404)
            if not verify_password(password, user.password_hash):
                self._audit(
                    session, action="mfa_disable", success=False,
                    subject_user_id=user.id, subject_email=user.email,
                    detail={"reason": "bad_password"},
                )
                session.commit()
                raise AuthError("Invalid credentials", status_code=401)
            user.mfa_enabled = False
            user.mfa_secret = None
            user.mfa_recovery_codes = None
            self._audit(
                session, action="mfa_disable", success=True,
                subject_user_id=user.id, subject_email=user.email,
            )
            session.commit()
            return {"mfa_enabled": False}
        finally:
            session.close()

    # Constant-time comparison helper — exposed so future flows (password
    # reset, internal-token verification) reuse the same primitive instead of
    # rolling their own and getting the timing wrong.
    @staticmethod
    def constant_time_equals(a: str, b: str) -> bool:
        return hmac.compare_digest(a or "", b or "")

    # --- password recovery ---

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Logged-in password change. Re-verifies the current password so a
        stolen access token alone can't lock the legitimate user out, then
        rotates the bcrypt hash. Recovery tokens issued before the change
        stay valid by design — the user can use one if they later forget the
        new password — but a future improvement could nullify them here."""
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active:
                raise AuthError("User not found", status_code=404)
            if not verify_password(current_password, user.password_hash):
                self._audit(
                    session, action="password_change", success=False,
                    subject_user_id=user.id, subject_email=user.email,
                    ip_address=ip_address, user_agent=user_agent,
                    detail={"reason": "bad_password"},
                )
                session.commit()
                raise AuthError("Invalid credentials", status_code=401)
            user.password_hash = hash_password(new_password)
            self._audit(
                session, action="password_change", success=True,
                subject_user_id=user.id, subject_email=user.email,
                ip_address=ip_address, user_agent=user_agent,
            )
            session.commit()
        finally:
            session.close()

    def request_password_reset(
        self,
        email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Begin the forgot-password flow. Always behaves the same regardless
        of whether the email exists — silent on missing accounts to avoid
        leaking which addresses are registered. A 6-digit OTP is generated,
        bcrypt-hashed into ``password_reset_tokens``, and emailed in plain
        text. The flow is OTP-based (no link) so the mobile app can finish
        the reset without deep-linking, and the same UX works on web."""
        normalized = (email or "").strip().lower()
        if not normalized:
            return
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(email=normalized).first()
            if not user or not user.is_active:
                # Audit the attempt (privileged-action visibility) but say
                # nothing back to the caller.
                self._audit(
                    session, action="password_reset_request", success=False,
                    subject_email=normalized,
                    ip_address=ip_address, user_agent=user_agent,
                    detail={"reason": "unknown_or_inactive"},
                )
                session.commit()
                return

            otp = _generate_email_otp()
            token_hash = bcrypt.hashpw(
                otp.encode("utf-8"),
                bcrypt.gensalt(rounds=settings.auth_bcrypt_rounds),
            ).decode("utf-8")
            ttl = settings.auth_password_reset_ttl_seconds
            session.add(PasswordResetToken(
                id=str(uuid.uuid4()),
                user_id=user.id,
                token_hash=token_hash,
                expires_at=_now() + timedelta(seconds=ttl),
                ip_address=ip_address,
                user_agent=user_agent,
            ))
            self._audit(
                session, action="password_reset_request", success=True,
                subject_user_id=user.id, subject_email=user.email,
                ip_address=ip_address, user_agent=user_agent,
            )
            session.commit()

            text_body = (
                "Someone (hopefully you) requested a password reset for your Ariadne account.\n\n"
                f"Your one-time code (valid for {ttl // 60} minutes):\n\n"
                f"    {otp}\n\n"
                "Enter it in the app along with your new password.\n\n"
                "If you did not request this, you can safely ignore this email."
            )
            if email_configured():
                send_email(
                    to=user.email,
                    subject="Your Ariadne password reset code",
                    text=text_body,
                )
        finally:
            session.close()

    def reset_password(
        self,
        email: str,
        code: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Redeem a 6-digit OTP. Looks up the user by email, then walks
        their outstanding tokens in constant time so timing doesn't leak
        which slot held the match. Marks the redeemed row used and rotates
        the user's password hash. Generic "Invalid or expired code" is used
        for every failure so the API can't be used to enumerate accounts."""
        normalized = (email or "").strip().lower()
        cleaned_code = (code or "").strip().replace(" ", "")
        if not normalized or not cleaned_code:
            raise AuthError("Invalid or expired code", status_code=400)
        session = self._SessionLocal()
        try:
            now = _now()
            user = session.query(AppUser).filter_by(email=normalized).first()
            if not user or not user.is_active:
                self._audit(
                    session, action="password_reset", success=False,
                    subject_email=normalized,
                    ip_address=ip_address, user_agent=user_agent,
                    detail={"reason": "unknown_user"},
                )
                session.commit()
                raise AuthError("Invalid or expired code", status_code=400)

            candidates = session.query(PasswordResetToken).filter(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            ).all()

            matched: PasswordResetToken | None = None
            candidate = cleaned_code.encode("utf-8")
            for row in candidates:
                try:
                    if bcrypt.checkpw(candidate, row.token_hash.encode("utf-8")):
                        matched = row
                        # Don't break — keep walking to flatten timing.
                except (ValueError, TypeError):
                    continue

            if not matched:
                self._audit(
                    session, action="password_reset", success=False,
                    subject_user_id=user.id, subject_email=user.email,
                    ip_address=ip_address, user_agent=user_agent,
                    detail={"reason": "invalid_or_expired_code"},
                )
                session.commit()
                raise AuthError("Invalid or expired code", status_code=400)

            user.password_hash = hash_password(new_password)
            matched.used_at = now
            self._audit(
                session, action="password_reset", success=True,
                subject_user_id=user.id, subject_email=user.email,
                ip_address=ip_address, user_agent=user_agent,
            )
            session.commit()
        finally:
            session.close()

    def guest_login(
        self,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """Issue a guest access token. Guests have no AppUser row, no
        org_id, and role='guest'; the JWT's ``sub`` is ``guest_<uuid>`` so
        future audit attribution and middleware identity stamping still
        work. Combined with the RLS policy that admits ``is_public = true``
        on campuses/buildings/floors, this restricts guests to public-flagged
        places (malls, airports) without needing a separate read path."""
        guest_id = f"guest_{uuid.uuid4()}"
        token, exp = issue_token(guest_id, organization_id=None, role="guest")
        session = self._SessionLocal()
        try:
            # Guests have no app_users row, so subject_user_id must stay NULL —
            # the FK on audit_log.subject_user_id rejects synthetic guest_<uuid>.
            # The id is preserved in detail for forensic attribution.
            self._audit(
                session, action="guest_login", success=True,
                subject_user_id=None,
                ip_address=ip_address, user_agent=user_agent,
                detail={"guest_id": guest_id},
            )
            session.commit()
        finally:
            session.close()
        return {
            "mfa_required": False,
            "user": {
                "id": guest_id,
                "email": "guest@ariadne.local",
                "full_name": "Guest",
            },
            "organization_id": None,
            "role": "guest",
            "token": token,
            "token_expires_at": exp.isoformat(),
        }

    def get_principal(self, user_id: str, organization_id: str | None) -> dict:
        """Look up the active user and resolve their effective role for the
        organization carried in their JWT. Used by GET /auth/me and by the
        middleware when it has decoded a JWT and needs to confirm the user
        is still active and still a member of the org.

        Guest principals (``user_id`` starts with ``guest_``) bypass the DB
        lookup since they have no ``app_users`` row by design."""
        if user_id.startswith("guest_"):
            return {
                "id": user_id,
                "email": "guest@ariadne.local",
                "full_name": "Guest",
                "organization_id": None,
                "role": "guest",
                "mfa_enabled": False,
                "mfa_method": None,
            }
        session = self._SessionLocal()
        try:
            user = session.query(AppUser).filter_by(id=user_id).first()
            if not user or not user.is_active:
                raise AuthError("User not found or inactive", status_code=401)

            role_value: str | None = None
            org_id: str | None = None
            if organization_id:
                membership = session.query(OrganizationMember).filter_by(
                    user_id=user_id, organization_id=organization_id,
                ).first()
                if membership:
                    org_id = membership.organization_id
                    role_value = membership.role.value
                else:
                    # Token claims an org the user is no longer in. Fail
                    # closed rather than silently strip the claim.
                    raise AuthError("No longer a member of that organization", status_code=403)

            return {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "organization_id": org_id,
                "role": role_value,
                "mfa_enabled": bool(user.mfa_enabled),
                "mfa_method": user.mfa_method or "totp",
            }
        finally:
            session.close()


def generate_default_jwt_secret() -> str:
    """Helper for first-time setup: print a 32-byte URL-safe secret to paste
    into AUTH_JWT_SECRET. Not called at runtime."""
    return secrets.token_urlsafe(32)
