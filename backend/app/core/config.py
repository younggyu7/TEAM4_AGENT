from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "itsm-design-agent"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://app:app@localhost:5432/app"
    storage_root: Path = Path(".storage")
    validation_database_url: SecretStr = SecretStr("")
    initialize_database: bool = False
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_max_tokens: int = Field(default=8192, ge=1, le=64000)
    anthropic_timeout_seconds: float = Field(default=120, gt=0, le=600)
    enable_prompt_caching: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_tracing_enabled: bool = True

    @model_validator(mode="after")
    def _validate_database_urls(self):
        validation_url = self.validation_database_url.get_secret_value()
        if validation_url:
            validation = urlsplit(validation_url)
            service = urlsplit(self.database_url)
            if (
                validation.scheme.startswith("postgresql")
                and service.scheme.startswith("postgresql")
                and (validation.hostname, validation.port or 5432, validation.path)
                == (service.hostname, service.port or 5432, service.path)
            ):
                raise ValueError("검증 DB는 서비스 저장 DB와 달라야 합니다")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
