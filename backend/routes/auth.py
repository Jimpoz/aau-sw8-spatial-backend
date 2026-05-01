from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from services.auth_service import AuthError, AuthService, decode_token

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    organization_id: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    organization_id: str | None = None


class MfaLoginRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6)


class MfaConfirmRequest(BaseModel):
    code: str = Field(min_length=6)


class MfaDisableRequest(BaseModel):
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class PasswordForgotRequest(BaseModel):
    email: EmailStr


class PasswordResetRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=12)
    new_password: str = Field(min_length=8)


class UserDTO(BaseModel):
    id: str
    email: str
    full_name: str | None = None


class AuthResponse(BaseModel):
    mfa_required: bool = False
    mfa_method: str | None = None
    challenge_token: str | None = None
    challenge_expires_at: str | None = None
    user: UserDTO | None = None
    organization_id: str | None = None
    role: str | None = None
    token: str | None = None
    token_expires_at: str | None = None


class MfaSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str
    recovery_codes: list[str]


class MfaEmailSetupResponse(BaseModel):
    setup_challenge_token: str
    challenge_expires_at: str
    recovery_codes: list[str]


class MfaEmailConfirmRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6, max_length=6)


class MfaStateResponse(BaseModel):
    mfa_enabled: bool
    mfa_method: str | None = None


class MeResponse(BaseModel):
    id: str
    email: str
    full_name: str | None = None
    organization_id: str | None = None
    role: str | None = None
    mfa_enabled: bool = False
    mfa_method: str | None = None


def _get_service() -> AuthService:
    try:
        return AuthService()
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    fwd = request.headers.get("x-forwarded-for")
    ip = (fwd.split(",")[0].strip() if fwd else None) or (
        request.client.host if request.client else None
    )
    return ip, request.headers.get("user-agent")


def _require_user_id(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token, expected_type="access")
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id


@router.post("/signup", response_model=AuthResponse, status_code=201)
def signup(payload: SignupRequest, request: Request, svc: AuthService = Depends(_get_service)):
    ip, ua = _client_meta(request)
    try:
        result = svc.signup(
            email=payload.email,
            password=payload.password,
            organization_id=payload.organization_id,
            full_name=payload.full_name,
            ip_address=ip,
            user_agent=ua,
        )
        return {**result, "mfa_required": False}
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/guest", response_model=AuthResponse)
def guest(request: Request, svc: AuthService = Depends(_get_service)):
    ip, ua = _client_meta(request)
    return svc.guest_login(ip_address=ip, user_agent=ua)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, svc: AuthService = Depends(_get_service)):
    ip, ua = _client_meta(request)
    try:
        return svc.login(
            email=payload.email,
            password=payload.password,
            organization_id=payload.organization_id,
            ip_address=ip,
            user_agent=ua,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/login/mfa", response_model=AuthResponse)
def login_mfa(payload: MfaLoginRequest, request: Request, svc: AuthService = Depends(_get_service)):
    ip, ua = _client_meta(request)
    try:
        result = svc.login_mfa(
            challenge_token=payload.challenge_token,
            code=payload.code,
            ip_address=ip,
            user_agent=ua,
        )
        return {**result, "mfa_required": False}
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/mfa/setup", response_model=MfaSetupResponse)
def mfa_setup(
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    try:
        return svc.setup_mfa(user_id)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/mfa/confirm", response_model=MfaStateResponse)
def mfa_confirm(
    payload: MfaConfirmRequest,
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    try:
        return svc.confirm_mfa(user_id, payload.code)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/mfa/disable", response_model=MfaStateResponse)
def mfa_disable(
    payload: MfaDisableRequest,
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    try:
        return svc.disable_mfa(user_id, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/mfa/email/setup", response_model=MfaEmailSetupResponse)
def mfa_email_setup(
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    try:
        return svc.setup_mfa_email(user_id)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/mfa/email/confirm", response_model=MfaStateResponse)
def mfa_email_confirm(
    payload: MfaEmailConfirmRequest,
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    try:
        return svc.confirm_mfa_email(user_id, payload.challenge_token, payload.code)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/password/change", status_code=204)
def password_change(
    payload: PasswordChangeRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    ip, ua = _client_meta(request)
    try:
        svc.change_password(
            user_id=user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            ip_address=ip,
            user_agent=ua,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/password/forgot", status_code=202)
def password_forgot(
    payload: PasswordForgotRequest,
    request: Request,
    svc: AuthService = Depends(_get_service),
):
    """Always returns 202 regardless of whether the email exists, to avoid
    leaking which addresses are registered."""
    ip, ua = _client_meta(request)
    svc.request_password_reset(
        email=payload.email, ip_address=ip, user_agent=ua,
    )
    return {"status": "ok"}


@router.post("/password/reset", status_code=204)
def password_reset(
    payload: PasswordResetRequest,
    request: Request,
    svc: AuthService = Depends(_get_service),
):
    ip, ua = _client_meta(request)
    try:
        svc.reset_password(
            email=payload.email,
            code=payload.code,
            new_password=payload.new_password,
            ip_address=ip,
            user_agent=ua,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/me", response_model=MeResponse)
def me(
    authorization: str | None = Header(default=None),
    svc: AuthService = Depends(_get_service),
):
    user_id = _require_user_id(authorization)
    try:
        token = authorization.split(" ", 1)[1].strip() if authorization else ""
        claims = decode_token(token, expected_type="access")
        return svc.get_principal(
            user_id=user_id,
            organization_id=claims.get("org_id"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
