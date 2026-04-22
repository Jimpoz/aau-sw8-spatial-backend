from pathlib import Path
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    supabase_url: str = "https://your-project.supabase.co"
    supabase_key: str = "your-anon-key"
    supabase_db_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_DB_URL", "SUPABASE_DATABASE_URL"),
    )
    supabase_enable_sync: bool = True

    model_config = {
        "env_file": str(Path(__file__).resolve().parents[2] / ".env"),
        "extra": "ignore",
    }


settings = Settings()
