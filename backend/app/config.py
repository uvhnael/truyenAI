from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")
    database_url: str = "postgresql+asyncpg://truyenai:truyenai@localhost:54329/truyenai"
    redis_url: str = "redis://localhost:63790/0"
    web_origin: str = "http://localhost:13000"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.5-flash"
    llm_mode: Literal["auto", "mock", "live"] = "auto"
    llm_timeout: int = Field(default=180, ge=5, le=600)
    llm_max_tokens: int = Field(default=16000, ge=1024, le=65536)
    max_revision_rounds: int = Field(default=2, ge=0, le=3)

    @property
    def mock(self) -> bool:
        return self.llm_mode == "mock" or (self.llm_mode == "auto" and not self.llm_api_key)


settings = Settings()
