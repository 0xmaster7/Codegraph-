from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    falkordb_host: str = "127.0.0.1"
    falkordb_port: int = Field(default=6379, ge=1, le=65535)
    falkordb_username: str | None = None
    falkordb_password: str | None = None
    allowed_repository_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    snapshot_cache_dir: Path = Path(".codegraph-cache/snapshots")
    embedding_cache_dir: Path = Path(".codegraph-cache/embeddings")
    hosted_embedding_enabled: bool = False
    hosted_embedding_cost_per_million_tokens_usd: Decimal = Field(
        default=Decimal("0.02"), gt=0, allow_inf_nan=False
    )
    max_hosted_embedding_run_cost_usd: Decimal = Field(
        default=Decimal("0.50"), ge=0, allow_inf_nan=False
    )
    openai_api_key: SecretStr | None = None
    local_query_analyzer_enabled: bool = True
    local_query_analyzer_url: str = "http://127.0.0.1:11434"
    local_query_analyzer_model: str = "qwen2.5:0.5b"
    failed_snapshot_gc_grace_seconds: int = Field(default=604_800, ge=0)
    max_source_file_bytes: int = Field(default=1_048_576, ge=1)
    scip_python_bin: str | None = None
    scip_cli_bin: str | None = None
    runtime_docker_bin: str = "docker"
    runtime_container_image: str | None = None
    runtime_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    runtime_memory_mb: int = Field(default=1024, ge=128, le=65_536)
    runtime_cpu_count: float = Field(default=1.0, gt=0, le=64)
    runtime_pids_limit: int = Field(default=128, ge=16, le=4096)
    runtime_output_limit_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    m21_max_commit_blob_bytes: int = Field(default=33_554_432, ge=1, le=536_870_912)
    m21_max_commit_materialized_bytes: int = Field(default=268_435_456, ge=1, le=2_147_483_648)
    m21_commit_materialization_timeout_seconds: int = Field(default=120, ge=1, le=900)

    @field_validator("app_host")
    @classmethod
    def local_by_default(cls, host: str) -> str:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("APP_HOST must be a loopback address for local V1")
        return host

    @field_validator("allowed_repository_roots", mode="before")
    @classmethod
    def parse_roots(cls, value: object) -> object:
        if isinstance(value, str):
            return [Path(part.strip()).expanduser() for part in value.split(",") if part.strip()]
        return value

    @field_validator("allowed_repository_roots")
    @classmethod
    def require_absolute_roots(cls, roots: list[Path]) -> list[Path]:
        expanded = [root.expanduser() for root in roots]
        if any(not root.is_absolute() for root in expanded):
            raise ValueError("ALLOWED_REPOSITORY_ROOTS entries must be absolute paths")
        return expanded

    @field_validator("local_query_analyzer_url")
    @classmethod
    def require_loopback_query_analyzer(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Local query analyzer URL must use unauthenticated HTTP loopback")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
