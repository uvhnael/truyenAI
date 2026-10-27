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
    # TTS engine = Kokoro-Vietnamese (torch). tts_model_dir: if set, resolve local
    # kokoro model + voicepacks from that directory (must contain config.json + kokoro_vi.pth);
    # if empty, Kokoro downloads from HuggingFace (contextboxai/Kokoro-Vietnamese) to the HF cache.
    tts_model_dir: str = ""
    tts_voice: str = "diem_trinh"
    tts_device: Literal["cpu", "cuda"] = "cpu"
    tts_gpu_device_id: int = Field(default=0, ge=0)
    # "short" = đúng 1 chương; "long" = nhiều chương như hiện tại
    default_mode: str = Field(default="long", pattern="^(short|long)$")
    audio_dir: str = "data/audio"
    video_dir: str = "data/videos"
    video_background_dir: str = "data/video-backgrounds"
    # OAuth app credentials for social publishing (TikTok Direct Post / YouTube Data API v3)
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    social_redirect_base: str = "http://localhost:18080"
    # OAuth URLs (may be set at runtime from a fresh authorize response in live mode)
    youtube_oauth_token_url: str = "https://oauth2.googleapis.com/token"
    youtube_oauth_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"

    @property
    def mock(self) -> bool:
        return self.llm_mode == "mock" or (self.llm_mode == "auto" and not self.llm_api_key)


settings = Settings()
