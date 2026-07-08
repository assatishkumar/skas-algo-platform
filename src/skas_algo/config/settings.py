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
    # Bind LOCALHOST by default: this is a single-user, real-money system with NO auth on
    # its routes (arm/force-entry/flatten/delete are all anonymous). Binding 0.0.0.0 would
    # expose those to anyone on the LAN — CORS does NOT protect a direct HTTP client. Only
    # the container path sets SKAS_API_HOST=0.0.0.0 explicitly (docker publishes a port).
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Secrets / encryption ---
    # Fernet key used to encrypt broker credentials & TOTP secrets at rest.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_encryption_key: str | None = None

    # --- App authentication (single operator password → JWT bearer) ---
    # FAIL-OPEN: auth is enforced only when BOTH of these are set (auth_enabled). Unset →
    # the API is open (localhost dev + the existing test suite are unchanged). A networked
    # host (the VPS) MUST set both, or it is unauthenticated.
    #   hash:   venv/bin/skas-algo hash-password
    #   secret: python -c "import secrets; print(secrets.token_urlsafe(48))"
    auth_password_hash: str | None = None   # SKAS_AUTH_PASSWORD_HASH (bcrypt)
    auth_jwt_secret: str | None = None      # SKAS_AUTH_JWT_SECRET (HS256 signing key)
    auth_token_ttl_hours: int = 24          # SKAS_AUTH_TOKEN_TTL_HOURS (login token lifetime)

    # --- Alerts (Telegram) ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- Trading Brain (Obsidian vault export) ---
    # When set, app activity (run-cards, journal) is exported as Markdown into this Obsidian
    # vault folder for the Claude-Desktop "trading brain". Unset → all vault export is a no-op.
    vault_path: str | None = None

    # --- Live trading safety ---
    # Master switch. Even with an account armed, no real order is placed unless this
    # is True. Defaults False so paper/dev never fires real orders by accident.
    live_trading_enabled: bool = False
    # Real-order safety rails (LiveBroker pre-flight; see brokers/live_broker.py).
    live_max_order_notional: float = 500_000.0   # SKAS_LIVE_MAX_ORDER_NOTIONAL
    live_max_orders_per_day: int = 20            # SKAS_LIVE_MAX_ORDERS_PER_DAY
    live_order_timeout_s: float = 10.0           # SKAS_LIVE_ORDER_TIMEOUT_S (LIMIT→MARKET)
    # Resume REAL-order management for a LIVE run after a restart/recovery. Default False =
    # fail-safe: a recovered live run keeps PaperBroker (a restart PAUSES real orders until
    # the owner re-activates). When True, recovery re-injects the LiveBroker — but the 4-key
    # gate still fully applies AND the run reconciles its broker book before its first
    # decision (reconcile_pending). Off unless the owner deliberately turns it on.
    live_resume_orders_on_recovery: bool = False  # SKAS_LIVE_RESUME_ORDERS_ON_RECOVERY

    # --- Live pricing feed (WebSocket) ---
    # When True (default), zerodha runs pull marks from a shared per-account KiteTicker
    # WebSocket feed (push) with a REST fallback on any staleness; False forces the legacy
    # per-run REST polling. Broker-agnostic surface — Dhan/cache paths are unaffected.
    ws_feed_enabled: bool = True
    ws_feed_stale_s: float = 10.0        # in-market: a mark older than this → REST fallback

    # --- Backups ---
    db_backup_keep: int = 7              # rolling on-box snapshots of the sqlite DB to retain
    # OFF-BOX durability: a shell command run AFTER the nightly snapshot to ship it off the
    # box (disk-failure protection). ``{path}`` = the snapshot's absolute path, ``{name}`` =
    # its filename. Unset → on-box only (unchanged). Destination-agnostic, e.g.:
    #   rsync -az {path} user@backup-host:/skas-backups/
    #   rclone copy {path} b2:my-bucket/skas-backups/
    #   aws s3 cp {path} s3://my-bucket/skas-backups/
    backup_remote_cmd: str | None = None   # SKAS_BACKUP_REMOTE_CMD

    # --- Logging ---
    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def auth_enabled(self) -> bool:
        """Auth is enforced only when both the password hash and the JWT secret are set."""
        return bool(self.auth_password_hash and self.auth_jwt_secret)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
