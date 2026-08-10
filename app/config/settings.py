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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.dfs_catalog import DFS_PROVIDER_NAME_SET
from app.domain.freshness import cache_window_policy, time_window_seconds
from app.domain.market_content import NumericDomainError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

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


def _provider_window(
    value: Decimal | Mapping[str, Decimal], provider: str, *, default: Decimal
) -> Decimal:
    """Resolve one scalar-or-provider-map cache setting."""

    if isinstance(value, Mapping):
        selected = value.get(str(provider).strip().casefold())
        if selected is not None:
            return selected
        if "*" in value:
            return value["*"]
        return default
    return value


def _cache_window_number(value: Any, *, field: str = "DFS cache windows") -> Decimal:
    """Parse one cache window through the shared exact time-window domain.

    The comparison board and the snapshot cache both read these windows through
    :func:`app.domain.freshness.time_window_seconds`, so the same authority
    decides here, once, at startup: a window this accepts can always be used by
    both, and a boolean, nonfinite, absurdly small, or absurdly large value is
    a configuration error rather than a request-time refusal.
    """

    return time_window_seconds(value, field=field)


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


class FeatureSettings(BaseModel):
    """Explicit exposure flags for features that are published separately.

    A flag is off unless an operator turns it on.  Publishing the DFS Board
    needs a prepared catalog, Redis policy, and provider credentials, so the
    route stays absent until the deployment says otherwise -- in every
    environment, including local development and tests.
    """

    model_config = ConfigDict(frozen=True)

    dfs_board_enabled: bool = False


class ProviderSettings(BaseModel):
    """Timeout, retry, pooling, and internal DFS board settings."""

    model_config = ConfigDict(frozen=True)

    nba_stats_timeout_seconds: float = Field(default=10.0, gt=0)
    nba_stats_max_concurrency: int = Field(default=10, ge=1, le=100)
    pbp_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    pbp_read_timeout_seconds: float = Field(default=30.0, gt=0)
    pbp_max_retries: int = Field(default=3, ge=0)
    pbp_pool_connections: int = Field(default=10, ge=1)
    pbp_pool_maxsize: int = Field(default=20, ge=1)
    dfs_enabled_providers: tuple[str, ...] = ()
    dfs_board_deadline_seconds: float = Field(default=15.0, gt=0, le=15.0)
    dfs_provider_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=3.0)
    dfs_provider_read_timeout_seconds: float = Field(default=8.0, gt=0, le=8.0)
    dfs_dabble_detail_concurrency: int = Field(default=3, ge=1, le=3)
    # The post-filter comparison-board ceiling.  A larger board is refused with
    # the observed count and the supported narrowing filters, never truncated.
    dfs_comparison_max_markets: int = Field(default=10000, ge=1)
    # A scalar applies to every enabled DFS provider.  A mapping may override
    # one or more providers when their publication cadence differs.  Both are
    # exact decimal seconds inside the shared time-window domain, so the window
    # a request compares an age against is the window an operator configured.
    dfs_cache_fresh_seconds: Decimal | dict[str, Decimal] = Field(default=Decimal(300))
    dfs_cache_stale_if_error_seconds: Decimal | dict[str, Decimal] = Field(
        default=Decimal(1800)
    )

    @field_validator("dfs_enabled_providers", mode="before")
    @classmethod
    def validate_dfs_enabled_providers(cls, value: Any) -> tuple[str, ...]:
        """Normalize the explicit DFS provider registry setting."""

        if value is None:
            return ()
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("DFS_ENABLED_PROVIDERS must be a comma-separated list")
        providers: list[str] = []
        seen: set[str] = set()
        for raw_provider in value:
            provider = str(raw_provider).strip().casefold()
            if not provider:
                continue
            if provider not in DFS_PROVIDER_NAME_SET:
                raise ValueError(
                    "DFS_ENABLED_PROVIDERS contains an unsupported provider: "
                    + provider
                )
            if provider not in seen:
                providers.append(provider)
                seen.add(provider)
        return tuple(providers)

    @field_validator(
        "dfs_cache_fresh_seconds", "dfs_cache_stale_if_error_seconds", mode="before"
    )
    @classmethod
    def validate_dfs_cache_window(cls, value: Any) -> Decimal | dict[str, Decimal]:
        if isinstance(value, Mapping):
            normalized: dict[str, Decimal] = {}
            for provider, window in value.items():
                name = str(provider).strip().casefold()
                if not name:
                    raise ValueError("DFS cache provider names must be non-empty")
                normalized[name] = _cache_window_number(window)
            return normalized
        return _cache_window_number(value)

    @model_validator(mode="after")
    def validate_dfs_cache_window_order(self) -> "ProviderSettings":
        """A fresh window can never outlast the age its provider may be served at.

        The ordering itself is stated once, by
        :func:`app.domain.freshness.cache_window_policy`, and is checked here
        for every provider the resolution can actually select -- so the policy
        a configuration accepts is exactly the policy a runtime cache accepts.
        """

        names = {
            *DFS_PROVIDER_NAME_SET,
            "*",
            *(
                self.dfs_cache_fresh_seconds
                if isinstance(self.dfs_cache_fresh_seconds, Mapping)
                else ()
            ),
            *(
                self.dfs_cache_stale_if_error_seconds
                if isinstance(self.dfs_cache_stale_if_error_seconds, Mapping)
                else ()
            ),
        }
        for name in sorted(names):
            cache_window_policy(
                self.dfs_cache_fresh_seconds_for(name),
                self.dfs_cache_stale_if_error_seconds_for(name),
                fresh_field=f"the DFS cache fresh window (provider {name!r})",
                stale_field="the stale-if-error age",
            )
        return self

    def dfs_cache_fresh_seconds_for(self, provider: str) -> Decimal:
        """Return the configured fresh window for one provider."""

        return _provider_window(
            self.dfs_cache_fresh_seconds, provider, default=Decimal(300)
        )

    def dfs_cache_stale_if_error_seconds_for(self, provider: str) -> Decimal:
        """Return the configured stale-if-error age for one provider."""

        return _provider_window(
            self.dfs_cache_stale_if_error_seconds,
            provider,
            default=Decimal(1800),
        )


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


#: Every configured catalog window, with the unit it is stated in and the
#: variable an operator writes it in.  Each one is a time window, so each one
#: is bounded by the same authority in the same place -- none of them reaches a
#: service through a float or an unchecked domain.
CATALOG_WINDOW_UNITS: dict[str, tuple[int, str]] = {
    "athlete_freshness_days": (86400, "ATHLETE_CATALOG_FRESHNESS_DAYS"),
    "event_max_age_hours": (3600, "EVENT_CATALOG_MAX_AGE_HOURS"),
    "event_match_window_hours": (3600, "EVENT_MAPPING_MATCH_WINDOW_HOURS"),
    "slate_schedule_max_age_hours": (3600, "SLATE_SCHEDULE_MAX_AGE_HOURS"),
    "player_game_log_max_age_hours": (3600, "PLAYER_GAME_LOG_MAX_AGE_HOURS"),
}


def _exact_quantity(value: Any, *, field: str) -> Decimal:
    """One configured quantity as the exact decimal it was written as.

    The value is read from its own digits rather than through a float, so a
    window an operator wrote is the window a service compares against, and an
    unsupported or unparsable setting is one typed domain error naming the
    variable rather than its value.
    """

    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise NumericDomainError(f"{field} must be a finite decimal")
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise NumericDomainError(f"{field} must be a finite decimal") from error


class CatalogSettings(BaseModel):
    """Independent freshness policies and reviewed matching windows."""

    model_config = ConfigDict(frozen=True)

    athlete_freshness_days: int = 7
    event_max_age_hours: Decimal = Decimal(72)
    slate_schedule_max_age_hours: Decimal = Decimal(30)
    player_game_log_max_age_hours: Decimal = Decimal(30)
    player_game_log_min_active_players_per_team_game: int = Field(
        default=5, strict=True, ge=1
    )
    matchup_selection_h2h_min_games: int = Field(default=1, strict=True, ge=1)
    matchup_selection_archetype_min_games: int = Field(
        default=5, strict=True, ge=1
    )
    #: How far a provider's reported start time may sit from a scheduled NBA
    #: game before the two are no longer evidence of the same event.  The
    #: boundary itself is inside the window.
    event_match_window_hours: Decimal = Decimal(6)

    @field_validator(*CATALOG_WINDOW_UNITS, mode="before")
    @classmethod
    def validate_catalog_window(cls, value: Any, info: ValidationInfo) -> Any:
        """Every catalog window is bounded once, in its own unit, at startup.

        A window this accepts can always be turned into the exact duration its
        service compares against, so nothing a configuration admitted is
        refused later by a request, and nothing absurd reaches a ``timedelta``.
        """

        unit_seconds, variable = CATALOG_WINDOW_UNITS[info.field_name]
        quantity = _exact_quantity(value, field=variable)
        time_window_seconds(quantity, unit_seconds=unit_seconds, field=variable)
        if info.field_name == "athlete_freshness_days":
            if quantity != quantity.to_integral_value():
                raise NumericDomainError(f"{variable} must be a whole number of days")
            return int(quantity)
        return quantity


class RuntimeSettings(BaseModel):
    """Complete, typed settings object created during application startup."""

    model_config = ConfigDict(frozen=True)

    environment: str = "development"
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthenticationSettings = Field(default_factory=AuthenticationSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    cors: CORSSettings = Field(default_factory=CORSSettings)
    nba: NBASeasonSettings = Field(default_factory=NBASeasonSettings)
    # Event-catalog freshness is intentionally independent from athlete
    # catalog state.  Operators may lengthen/shorten the read-age window
    # without changing the provider timeout or a worker schedule.
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)
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
        if isinstance(value, bool):
            # ``float(True)`` would silently accept a boolean as 1.0.
            raise ConfigurationError(f"{name} must be a number, got {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"{name} must be a number, got {value!r}"
            ) from error


def _configured_window(
    reader: _EnvironmentReader, name: str, default: Decimal | None
) -> Decimal:
    """Read one configured time window exactly, or fail startup naming it.

    The raw text is converted straight to an exact decimal rather than through
    a float, so the window a request compares against is the one written in the
    environment, and the refusal names the variable rather than its value.
    """

    value = reader.raw(name)
    if value is None and default is not None:
        return default
    try:
        return _cache_window_number(value, field=name)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def _build_settings(
    reader: _EnvironmentReader,
) -> RuntimeSettings:
    """Build and validate the nested settings model from one environment."""

    environment = reader.text("FLASK_ENV", "development") or "development"
    database_url = reader.text("DATABASE_URL", DEFAULT_SQLITE_URL) or DEFAULT_SQLITE_URL

    configured_dfs_providers = reader.raw("DFS_ENABLED_PROVIDERS")

    dfs_cache_fresh = _configured_window(
        reader, "DFS_CACHE_FRESH_SECONDS", Decimal(300)
    )
    dfs_cache_stale = _configured_window(
        reader, "DFS_CACHE_STALE_IF_ERROR_SECONDS", Decimal(1800)
    )
    fresh_overrides: dict[str, Decimal] = {}
    stale_overrides: dict[str, Decimal] = {}
    for provider_name in DFS_PROVIDER_NAME_SET:
        env_name = provider_name.upper()
        for suffix, overrides in (
            ("CACHE_FRESH_SECONDS", fresh_overrides),
            ("CACHE_STALE_IF_ERROR_SECONDS", stale_overrides),
        ):
            variable = f"DFS_{env_name}_{suffix}"
            if reader.raw(variable) is not None:
                overrides[provider_name] = _configured_window(reader, variable, None)

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
    features = _validated_model(
        FeatureSettings,
        dfs_board_enabled=reader.boolean("DFS_BOARD_ENABLED", False),
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
        dfs_enabled_providers=configured_dfs_providers,
        dfs_board_deadline_seconds=reader.decimal(
            "DFS_BOARD_DEADLINE_SECONDS", 15.0
        ),
        dfs_provider_connect_timeout_seconds=reader.decimal(
            "DFS_PROVIDER_CONNECT_TIMEOUT_SECONDS", 3.0
        ),
        dfs_provider_read_timeout_seconds=reader.decimal(
            "DFS_PROVIDER_READ_TIMEOUT_SECONDS", 8.0
        ),
        dfs_dabble_detail_concurrency=reader.integer(
            "DFS_DABBLE_DETAIL_CONCURRENCY", 3
        ),
        dfs_comparison_max_markets=reader.integer(
            "DFS_COMPARISON_MAX_MARKETS", 10000
        ),
        dfs_cache_fresh_seconds=(
            {"*": dfs_cache_fresh, **fresh_overrides}
            if fresh_overrides
            else dfs_cache_fresh
        ),
        dfs_cache_stale_if_error_seconds=(
            {"*": dfs_cache_stale, **stale_overrides}
            if stale_overrides
            else dfs_cache_stale
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
            features=features,
            providers=providers,
            llm=llm,
            cors=cors,
            nba=_validated_model(NBASeasonSettings),
            # Every catalog window is read as written and bounded by the one
            # time-window authority, so none of them is ever rounded through a
            # float on the way to the duration a service compares against.
            catalog=_validated_model(
                CatalogSettings,
                athlete_freshness_days=reader.raw(
                    "ATHLETE_CATALOG_FRESHNESS_DAYS", 7
                ),
                event_max_age_hours=reader.raw(
                    "EVENT_CATALOG_MAX_AGE_HOURS", Decimal(72)
                ),
                event_match_window_hours=reader.raw(
                    "EVENT_MAPPING_MATCH_WINDOW_HOURS", Decimal(6)
                ),
                slate_schedule_max_age_hours=reader.raw(
                    "SLATE_SCHEDULE_MAX_AGE_HOURS", Decimal(30)
                ),
                player_game_log_max_age_hours=reader.raw(
                    "PLAYER_GAME_LOG_MAX_AGE_HOURS", Decimal(30)
                ),
                player_game_log_min_active_players_per_team_game=reader.integer(
                    "PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME", 5
                ),
                matchup_selection_h2h_min_games=reader.integer(
                    "MATCHUP_SELECTION_H2H_MIN_GAMES", 1
                ),
                matchup_selection_archetype_min_games=reader.integer(
                    "MATCHUP_SELECTION_ARCHETYPE_MIN_GAMES", 5
                ),
            ),
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
        if not settings.providers.dfs_enabled_providers:
            errors.append(
                "DFS_ENABLED_PROVIDERS must explicitly configure at least one provider"
            )
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

    # Publishing the board needs both halves of its configuration.  A flag with
    # no registry behind it would expose a route that can never call a provider,
    # so it fails startup in every environment rather than at a later request.
    if (
        settings.features.dfs_board_enabled
        and not settings.providers.dfs_enabled_providers
    ):
        errors.append(
            "DFS_BOARD_ENABLED=true requires DFS_ENABLED_PROVIDERS to name at "
            "least one provider"
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
    "CatalogSettings",
    "CORSSettings",
    "ConfigurationError",
    "DatabaseSettings",
    "FeatureSettings",
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
