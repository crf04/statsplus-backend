"""Validated runtime configuration for the StatsPlus application.

The application has a number of optional integrations, but it should still
have one predictable configuration boundary.  This module is that boundary:
environment variables are converted once into typed settings and the rest of
the application consumes the resulting object.

The defaults intentionally keep a fresh checkout useful without credentials:
the bundled SQLite database is used, Redis and OpenAI are optional, and the
Firebase bypass is opt-in for local development/tests only.  Production is
different: a real database and Firebase credentials are required and a bad
configuration fails startup with :class:`ConfigurationError`.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_SQLITE_URL = "sqlite:///nba_play_types.db"
LOCAL_ENVIRONMENTS = frozenset({"development", "testing", "test", "local"})
SUPPORTED_ENVIRONMENTS = frozenset({*LOCAL_ENVIRONMENTS, "staging", "production"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
DEFAULT_LOCAL_CORS_ORIGINS = ("http://localhost:3000",)


class ConfigurationError(ValueError):
    """Raised when runtime configuration cannot safely start the app."""


def current_nba_season(today: date | None = None) -> str:
    """Return the NBA season containing ``today``.

    NBA regular seasons cross calendar years and begin in October.  Keeping
    this rule in one small, injectable function makes season defaults stable
    for every request module and straightforward to test at the boundary.
    """

    current_date = today or date.today()
    start_year = (
        current_date.year if current_date.month >= 10 else current_date.year - 1
    )
    return f"{start_year}-{str(start_year + 1)[-2:]}"


class DatabaseSettings(BaseModel):
    """Database connection settings."""

    model_config = ConfigDict(frozen=True)

    url: str = DEFAULT_SQLITE_URL


class AuthenticationSettings(BaseModel):
    """Firebase Admin settings and the explicit local bypass."""

    model_config = ConfigDict(frozen=True)

    firebase_admin_disabled: bool = False
    firebase_service_account_path: str | None = None
    firebase_service_account_json: str | None = None
    firebase_project_id: str | None = None
    firebase_private_key: str | None = None
    firebase_client_email: str | None = None

    @property
    def has_credentials(self) -> bool:
        """Whether one supported Firebase credential source is configured."""

        has_file = bool(self.firebase_service_account_path)
        has_json = bool(self.firebase_service_account_json)
        has_parts = all(
            (
                self.firebase_project_id,
                self.firebase_private_key,
                self.firebase_client_email,
            )
        )
        return has_file or has_json or has_parts


class CacheSettings(BaseModel):
    """Optional Redis cache settings."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    url: str | None = None
    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    password: str | None = None
    tls: bool = False


class ProviderSettings(BaseModel):
    """Timeout, retry, pooling, and fan-out settings for external providers."""

    model_config = ConfigDict(frozen=True)

    nba_stats_timeout_seconds: float = Field(default=10.0, gt=0)
    nba_stats_max_concurrency: int = Field(default=10, ge=1, le=100)
    pbp_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    pbp_read_timeout_seconds: float = Field(default=30.0, gt=0)
    pbp_max_retries: int = Field(default=3, ge=0)
    pbp_pool_connections: int = Field(default=10, ge=1)
    pbp_pool_maxsize: int = Field(default=20, ge=1)
    dabble_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    dabble_read_timeout_seconds: float = Field(default=30.0, gt=0)
    dabble_max_retries: int = Field(default=2, ge=0)
    dabble_pool_connections: int = Field(default=5, ge=1)
    dabble_pool_maxsize: int = Field(default=10, ge=1)
    dabble_max_fixtures_per_request: int = Field(default=5, ge=1, le=20)


class LLMSettings(BaseModel):
    """Optional OpenAI fallback settings."""

    model_config = ConfigDict(frozen=True)

    api_key: str | None = None
    model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    enable_fallback: bool = False
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)


def _normalize_cors_origin(value: Any) -> str:
    """Validate and normalize one exact browser origin."""

    if not isinstance(value, str):
        raise ValueError("CORS_ALLOWED_ORIGINS entries must be strings")

    origin = value.strip()
    if not origin or "*" in origin:
        raise ValueError(
            "CORS_ALLOWED_ORIGINS must contain explicit http:// or https:// origins"
        )

    parsed = urlsplit(origin)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"CORS_ALLOWED_ORIGINS contains an invalid origin: {value!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"CORS_ALLOWED_ORIGINS contains an invalid origin: {value!r}")
    if parsed.path not in {"", "/"}:
        raise ValueError(
            f"CORS_ALLOWED_ORIGINS contains an origin with a path: {value!r}"
        )

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(
            f"CORS_ALLOWED_ORIGINS contains an invalid port: {value!r}"
        ) from error

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"CORS_ALLOWED_ORIGINS contains an invalid origin: {value!r}")

    hostname = hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port_suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme.lower()}://{hostname}{port_suffix}"


def _parse_cors_origins(value: Any) -> tuple[str, ...]:
    """Parse a comma-separated or JSON-list origin setting."""

    if value is None:
        return DEFAULT_LOCAL_CORS_ORIGINS

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must be comma-separated origins or a JSON list"
                ) from error
        else:
            value = text.split(",")

    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(
            "CORS_ALLOWED_ORIGINS must be comma-separated origins or a JSON list"
        )

    origins: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _normalize_cors_origin(item)
        if normalized not in seen:
            origins.append(normalized)
            seen.add(normalized)

    if not origins:
        raise ValueError("CORS_ALLOWED_ORIGINS must contain at least one origin")
    return tuple(origins)


class CORSSettings(BaseModel):
    """Exact browser origins allowed to make cross-origin requests."""

    model_config = ConfigDict(frozen=True)

    allowed_origins: tuple[str, ...] = Field(default=DEFAULT_LOCAL_CORS_ORIGINS)

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def validate_allowed_origins(cls, value: Any) -> tuple[str, ...]:
        return _parse_cors_origins(value)


class NBASeasonSettings(BaseModel):
    """NBA season defaults shared by game-log and NL request paths."""

    model_config = ConfigDict(frozen=True)

    current_season: str = Field(default_factory=current_nba_season)


class RuntimeSettings(BaseModel):
    """Complete, typed settings object created during application startup."""

    model_config = ConfigDict(frozen=True)

    environment: str = "development"
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthenticationSettings = Field(default_factory=AuthenticationSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    cors: CORSSettings = Field(default_factory=CORSSettings)
    nba: NBASeasonSettings = Field(default_factory=NBASeasonSettings)
    port: int = Field(default=5000, ge=1, le=65535)
    debug: bool = True
    log_level: str = "INFO"

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> str:
        normalized = str(value or "development").strip().lower()
        if normalized == "test":
            normalized = "testing"
        if normalized not in SUPPORTED_ENVIRONMENTS:
            raise ValueError(
                f"FLASK_ENV must be one of {sorted(SUPPORTED_ENVIRONMENTS)}, "
                f"got {value!r}"
            )
        return normalized


class _EnvironmentReader:
    """Read environment values while keeping parsing errors actionable."""

    def __init__(self, values: Mapping[str, Any], overrides: Mapping[str, Any] | None):
        self.values = values
        self.overrides = overrides or {}

    def raw(self, name: str, default: Any = None, *aliases: str) -> Any:
        for candidate in (name, *aliases):
            if candidate in self.overrides:
                return self.overrides[candidate]
            if candidate in self.values:
                return self.values[candidate]
        return default

    def text(self, name: str, default: str | None = None, *aliases: str) -> str | None:
        value = self.raw(name, default, *aliases)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def boolean(self, name: str, default: bool = False, *aliases: str) -> bool:
        value = self.raw(name, default, *aliases)
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
        raise ConfigurationError(
            f"{name} must be a boolean (true/false), got {value!r}"
        )

    def integer(self, name: str, default: int, *aliases: str) -> int:
        value = self.raw(name, default, *aliases)
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"{name} must be an integer, got {value!r}"
            ) from error

    def decimal(self, name: str, default: float, *aliases: str) -> float:
        value = self.raw(name, default, *aliases)
        try:
            return float(value)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"{name} must be a number, got {value!r}"
            ) from error


def _build_settings(
    reader: _EnvironmentReader,
) -> RuntimeSettings:
    """Build and validate the nested settings model from one environment."""

    environment = reader.text("FLASK_ENV", "development") or "development"
    database_url = reader.text("DATABASE_URL", DEFAULT_SQLITE_URL) or DEFAULT_SQLITE_URL

    auth = _validated_model(
        AuthenticationSettings,
        firebase_admin_disabled=reader.boolean("FIREBASE_ADMIN_DISABLED", False),
        firebase_service_account_path=reader.text("FIREBASE_SERVICE_ACCOUNT_PATH"),
        firebase_service_account_json=reader.text("FIREBASE_SERVICE_ACCOUNT_JSON"),
        firebase_project_id=reader.text("FIREBASE_PROJECT_ID"),
        firebase_private_key=reader.text("FIREBASE_PRIVATE_KEY"),
        firebase_client_email=reader.text("FIREBASE_CLIENT_EMAIL"),
    )
    cache = _validated_model(
        CacheSettings,
        enabled=reader.boolean("ENABLE_CACHE", True),
        url=reader.text("REDIS_URL"),
        host=reader.text("REDISHOST", "localhost", "REDIS_HOST") or "localhost",
        port=reader.integer("REDISPORT", 6379, "REDIS_PORT"),
        database=reader.integer("REDISDB", 0, "REDIS_DB"),
        password=reader.text("REDISPASSWORD", None, "REDIS_PASSWORD"),
        tls=reader.boolean("REDISTLS", False, "REDIS_TLS"),
    )
    providers = _validated_model(
        ProviderSettings,
        nba_stats_timeout_seconds=reader.decimal("NBA_STATS_TIMEOUT_SECONDS", 10.0),
        nba_stats_max_concurrency=reader.integer("NBA_STATS_MAX_CONCURRENCY", 10),
        pbp_connect_timeout_seconds=reader.decimal("NBA_API_TIMEOUT_CONNECT", 10.0),
        pbp_read_timeout_seconds=reader.decimal("NBA_API_TIMEOUT_READ", 30.0),
        pbp_max_retries=reader.integer("NBA_API_MAX_RETRIES", 3),
        pbp_pool_connections=reader.integer("NBA_API_POOL_CONNECTIONS", 10),
        pbp_pool_maxsize=reader.integer("NBA_API_POOL_MAXSIZE", 20),
        dabble_connect_timeout_seconds=reader.decimal(
            "DABBLE_CONNECT_TIMEOUT_SECONDS", 5.0
        ),
        dabble_read_timeout_seconds=reader.decimal(
            "DABBLE_READ_TIMEOUT_SECONDS", 30.0
        ),
        dabble_max_retries=reader.integer("DABBLE_MAX_RETRIES", 2),
        dabble_pool_connections=reader.integer("DABBLE_POOL_CONNECTIONS", 5),
        dabble_pool_maxsize=reader.integer("DABBLE_POOL_MAXSIZE", 10),
        dabble_max_fixtures_per_request=reader.integer(
            "DABBLE_MAX_FIXTURES_PER_REQUEST", 5
        ),
    )

    api_key = reader.text("OPENAI_API_KEY")
    requested_llm_fallback = reader.boolean("ENABLE_LLM_FALLBACK", True)
    llm = _validated_model(
        LLMSettings,
        api_key=api_key,
        model=reader.text("LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        temperature=reader.decimal("LLM_TEMPERATURE", 0.0),
        max_tokens=reader.integer("LLM_MAX_TOKENS", 512),
        timeout_seconds=reader.decimal("LLM_TIMEOUT", 10.0),
        max_retries=reader.integer("LLM_MAX_RETRIES", 3),
        # The optional fallback is safe by default: an absent key disables it
        # even when an old .env file still says ENABLE_LLM_FALLBACK=true.
        enable_fallback=requested_llm_fallback and bool(api_key),
        confidence_threshold=reader.decimal("LLM_CONFIDENCE_THRESHOLD", 0.7),
    )
    cors_origins = reader.raw("CORS_ALLOWED_ORIGINS")
    cors = _validated_model(
        CORSSettings,
        **({"allowed_origins": cors_origins} if cors_origins is not None else {}),
    )

    try:
        settings = RuntimeSettings(
            environment=environment,
            database=_validated_model(DatabaseSettings, url=database_url),
            auth=auth,
            cache=cache,
            providers=providers,
            llm=llm,
            cors=cors,
            nba=_validated_model(NBASeasonSettings),
            port=reader.integer("PORT", 5000),
            debug=reader.boolean("FLASK_DEBUG", True),
            log_level=reader.text("LOG_LEVEL", "INFO") or "INFO",
        )
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise ConfigurationError(
            f"Invalid runtime configuration: {problems}"
        ) from error

    _validate_environment_requirements(
        settings,
        cors_origins_configured=cors_origins is not None,
    )
    return settings


def _validated_model(model_type: type[BaseModel], **values: Any) -> BaseModel:
    """Convert Pydantic field errors into the public config exception."""

    try:
        return model_type(**values)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise ConfigurationError(
            f"Invalid {model_type.__name__} configuration: {problems}"
        ) from error


def _validate_environment_requirements(
    settings: RuntimeSettings,
    *,
    cors_origins_configured: bool | None = None,
) -> None:
    """Enforce settings that are required for a safe production process."""

    errors: list[str] = []
    if settings.environment == "production":
        if cors_origins_configured is None:
            cors_origins_configured = (
                settings.cors.allowed_origins != DEFAULT_LOCAL_CORS_ORIGINS
            )
        if (
            not cors_origins_configured
            or settings.cors.allowed_origins == DEFAULT_LOCAL_CORS_ORIGINS
        ):
            errors.append(
                "CORS_ALLOWED_ORIGINS must be set to an explicit production allowlist"
            )
        if settings.database.url == DEFAULT_SQLITE_URL:
            errors.append("DATABASE_URL must be set to a production database URL")

        if settings.auth.firebase_admin_disabled:
            errors.append("FIREBASE_ADMIN_DISABLED must be false in production")
        elif not settings.auth.has_credentials:
            errors.append(
                "Firebase credentials are required in production "
                "(FIREBASE_SERVICE_ACCOUNT_JSON/PATH or the three individual fields)"
            )
        elif (
            settings.auth.firebase_service_account_path
            and not Path(settings.auth.firebase_service_account_path).is_file()
        ):
            errors.append(
                "FIREBASE_SERVICE_ACCOUNT_PATH must point to an existing file in production"
            )

        if settings.auth.firebase_service_account_json:
            try:
                parsed = json.loads(settings.auth.firebase_service_account_json)
                if not isinstance(parsed, dict):
                    errors.append(
                        "FIREBASE_SERVICE_ACCOUNT_JSON must contain a JSON object"
                    )
                else:
                    missing = sorted(
                        {"project_id", "private_key", "client_email"} - parsed.keys()
                    )
                    if missing:
                        errors.append(
                            "FIREBASE_SERVICE_ACCOUNT_JSON is missing required fields: "
                            + ", ".join(missing)
                        )
            except json.JSONDecodeError as error:
                errors.append(
                    f"FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON ({error.msg})"
                )

    if (
        settings.auth.firebase_admin_disabled
        and settings.environment not in LOCAL_ENVIRONMENTS
    ):
        errors.append(
            "FIREBASE_ADMIN_DISABLED=true is allowed only in local development or testing"
        )

    if errors:
        raise ConfigurationError("Invalid runtime configuration: " + "; ".join(errors))


def load_settings(
    environ: Mapping[str, Any] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> RuntimeSettings:
    """Load one validated :class:`RuntimeSettings` object.

    ``environ`` is injectable for deterministic tests.  Flask app config values
    can be supplied via ``overrides`` and take precedence over process
    environment variables.  The function does not mutate ``os.environ``.
    """

    values = os.environ if environ is None else environ
    return _build_settings(_EnvironmentReader(values, overrides))


_startup_settings: RuntimeSettings | None = None


def set_runtime_settings(settings: RuntimeSettings) -> None:
    """Expose the startup settings to import-time infrastructure adapters."""

    global _startup_settings
    _startup_settings = settings


def get_runtime_settings() -> RuntimeSettings:
    """Return the current app's settings, or the startup settings outside a request."""

    try:
        from flask import current_app

        app_settings = current_app.extensions.get("runtime_settings")
        if isinstance(app_settings, RuntimeSettings):
            return app_settings
    except (ImportError, RuntimeError):
        pass

    if _startup_settings is not None:
        return _startup_settings
    return load_settings()


__all__ = [
    "AuthenticationSettings",
    "CacheSettings",
    "CORSSettings",
    "ConfigurationError",
    "DatabaseSettings",
    "LLMSettings",
    "NBASeasonSettings",
    "ProviderSettings",
    "RuntimeSettings",
    "current_nba_season",
    "DEFAULT_LOCAL_CORS_ORIGINS",
    "get_runtime_settings",
    "load_settings",
    "set_runtime_settings",
]
