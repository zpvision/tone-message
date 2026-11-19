from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FunctionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    num_ctx: int = Field(default=8192, ge=1024, le=32768)
    max_output: int = Field(default=128, ge=16, le=4096)
    temperature: float = Field(default=0, ge=0, le=2)
    thinking: bool = False
    timeout: float = Field(default=180, gt=0, le=600)
    workers: int = Field(default=1, ge=1, le=4)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINKOPER_", extra="ignore")

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8000, ge=1024, le=65535)
    environment: str = "production"
    inference_url: str | None = None
    model_id: str | None = None
    connect_timeout_seconds: float = Field(default=5, gt=0)
    request_timeout_seconds: float = Field(default=120, gt=0)
    readiness_timeout_seconds: float = Field(default=2, gt=0, le=10)
    cabinet_enabled: bool = False
    cabinet_data_dir: str = "/var/lib/finkoper-ai/cabinet"
    ollama_num_parallel: int = Field(default=1, ge=1, le=4)
    # Retained only so older installations can start; native counting ignores it.
    context_tokenizer_dir: str = ""
    inference_backend: Literal["ollama"] = "ollama"
    model_digest: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    inference_keep_alive: str = "30m"
    inference_concurrency: int = Field(default=1, ge=1, le=4)
    inference_lock_dir: str = "/var/lib/finkoper-ai/gpu-locks"
    inference_lock_timeout: float = Field(default=30, gt=0, le=600)
    inference_retries: int = Field(default=1, ge=0, le=2)
    inference_retry_delay: float = Field(default=.2, ge=0, le=5)
    sentiment_workers: int = Field(default=1, ge=1, le=4)
    sentiment_temperature: float = Field(default=0, ge=0, le=2)
    sentiment_max_output: int = Field(default=128, ge=16, le=512)
    sentiment_thinking: bool = False
    tokenizer_timeout: float = Field(default=180, gt=0, le=180)
    tokenizer_discovery_ttl: float = Field(default=30, gt=0, le=300)
    # Public compatibility identifiers never select an inference model.
    api_model_alias: str = "qwen3:8b"
    api_model_aliases: tuple[str, ...] = ()
    task_priorities: dict[str, int] = {"sentiment": 0, "morning_briefing": 0, "assistant": 0}
    task_aging_seconds: float = Field(default=10, gt=0, le=60)

    function_profiles: dict[str, FunctionProfile] = {}

    @property
    def keep_alive(self):
        # Ollama accepts numeric -1/0; "-1" is not a valid duration string.
        value = self.inference_keep_alive
        return int(value) if value in ("-1", "0") else value

    def profile(self, name="sentiment"):
        from app.inference import InferenceProfile
        if name != "sentiment":
            if name not in self.function_profiles:
                raise ValueError("profile_not_configured: " + name)
            return InferenceProfile(name=name, **self.function_profiles[name].model_dump())
        return InferenceProfile(name=name, num_ctx=self.api_num_ctx,
            max_output=self.sentiment_max_output, temperature=self.sentiment_temperature,
            thinking=self.sentiment_thinking, timeout=self.api_inference_timeout,
            workers=self.sentiment_workers)

    @field_validator("inference_url")
    @classmethod
    def safe_url(cls, value):
        if value:
            from urllib.parse import urlsplit
            u = urlsplit(value)
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError("inference_url must be an HTTP URL without credentials/query")
        return value.rstrip("/") if value else value

    @field_validator("inference_keep_alive")
    @classmethod
    def keep_alive_valid(cls, value):
        import re
        if not re.fullmatch(r"(?:-1|0|[1-9][0-9]*(?:s|m|h))", value):
            raise ValueError("keep_alive must be -1, 0, or a positive duration (s/m/h)")
        return value

    @model_validator(mode="after")
    def inference_valid(self):
        if (self.api_enabled or self.cabinet_enabled) and (not self.inference_url or not self.model_id):
            raise ValueError("inference_url and exact model_id are required; no model fallback")
        if self.sentiment_workers > self.inference_concurrency:
            raise ValueError("sentiment_workers must not exceed inference_concurrency")
        if any(p.workers > self.inference_concurrency for p in self.function_profiles.values()):
            raise ValueError("profile workers exceed inference_concurrency")
        return self
    api_enabled: bool = False
    api_num_ctx: int = Field(default=4096, ge=1024, le=32768)
    api_data_dir: str = "/var/lib/finkoper-ai/api"
    api_max_items: int = Field(default=1000, ge=1, le=1000)
    api_max_body_bytes: int = Field(default=5*1024*1024, ge=1024, le=5*1024*1024)
    api_max_waiting_batches: int = Field(default=5, ge=0, le=20)
    api_max_waiting_messages: int = Field(default=2000, ge=1, le=20000)
    api_queue_timeout: float = Field(default=30, gt=0, le=600)
    api_deadline: float = Field(default=600, gt=0, le=3600)
    api_inference_timeout: float = Field(default=180, gt=0, le=600)

    # Reserved for a future implementation. These settings enable no features.
    api_key: SecretStr | None = Field(default=None, repr=False)
    max_batch_size: int | None = Field(default=None, ge=1)
    max_text_characters: int | None = Field(default=None, ge=1)
    max_queue_size: int | None = Field(default=None, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)

    @field_validator("inference_url", "model_id", "model_digest", "api_key", mode="before")
    @classmethod
    def empty_to_none(cls, value):
        return None if value == "" else value
