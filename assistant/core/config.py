from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    hf_token: str = ""
    assistant_mode: str = "offline"
    assistant_online_model_id: str = ""
    assistant_offline_model_id: str = "HuggingFaceTB/SmolLM2-360M-Instruct"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
