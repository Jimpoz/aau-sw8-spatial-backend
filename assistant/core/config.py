from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    hf_token: str = ""
    assistant_mode: str = "offline"
    assistant_online_model_id: str = ""
    assistant_offline_model_id: str = "HuggingFaceTB/SmolLM2-360M-Instruct"

    supabase_db_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_DB_URL", "SUPABASE_DATABASE_URL"),
    )

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
