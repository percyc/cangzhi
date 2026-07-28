from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://cangzhi:cangzhi-dev@localhost:5432/cangzhi"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    ollama_base_url: str = "http://localhost:11434"
    storage_path: str = "./storage"
    poll_interval: int = 5
    log_level: str = "info"


settings = Settings()
