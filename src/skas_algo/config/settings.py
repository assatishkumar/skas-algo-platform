"""Application settings, loaded from environment / .env.

Secrets (broker credentials, TOTP, tokens) are NEVER committed. They are read from
the environment or a local .env file (git-ignored). See .env.example for the shape.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Platform configuration.

    All values can be overridden via environment variables prefixed with ``SKAS_``
    (e.g. ``SKAS_DATABASE_URL``) or via a local ``.env`` file.
    """

    model_config = SettingsConfigDict(
        env_prefix="SKAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "skas-algo-platform"
    environment: str = Field(default="development")  # development | production | test
    debug: bool = True

    # --- Database (platform state; market data lives in skas-data's DuckDB) ---
    # Defaults to a local SQLite file for dev; use Postgres on the VPS.
    database_url: str = Field(default="sqlite:///./skas_algo.db")

    # --- API server ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Secrets / encryption ---
    # Fernet key used to encrypt broker credentials & TOTP secrets at rest.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_encryption_key: str | None = None

    # --- Logging ---
    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
