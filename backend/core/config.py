from pathlib import Path
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    supabase_db_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_DB_URL", "SUPABASE_DATABASE_URL"),
    )
    supabase_enable_sync: bool = True

    auth_jwt_secret: str | None = None
    auth_jwt_issuer: str = "ariadne-backend"
    auth_jwt_ttl_seconds: int = 60 * 60 * 12  # 12h
    auth_bcrypt_rounds: int = 12
    auth_rls_enabled: bool = False

    email_service_url: str | None = None
    internal_email_token: str | None = None
    auth_login_rate_limit: int = 10
    auth_mfa_challenge_ttl_seconds: int = 300
    auth_password_reset_ttl_seconds: int = 60 * 60
    auth_password_reset_url_template: str = "ariadne://reset-password?token={token}"

    model_config = {
        "env_file": str(Path(__file__).resolve().parents[2] / ".env"),
        "extra": "ignore",
    }


settings = Settings()
