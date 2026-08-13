"""One-shot residential collector orchestration and conditional outcomes."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from .client import CollectorHTTPError, CollectorToken, RailwayClient
from .cache import InstructionCache
from .contracts import CatalogEnvelope, ObservationEnvelope, ProviderContractError, parse_datetime
from .diagnostics import SafeStatus, log_status, safe_code
from .outbox import OutboxBusy, OutboxError, OutboxFull, OutboxItem, OutboxRepository, OutboxRetentionError
from .provider import ProviderTransientError, ResidentialScopeExecutor, ScopeWork


EXIT_NO_WORK = 0
EXIT_RETRY = 10
EXIT_BUSY = 11
EXIT_NON_RETRYABLE = 20
RUN_LEASE_SECONDS = 8 * 60 * 60


class RunDisposition(str, Enum):
    NO_WORK = "no_work"
    COMPLETE = "complete"
    RETRY = "retry"
    NON_RETRYABLE = "non_retryable"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class RunResult:
    disposition: RunDisposition
    exit_code: int
    discovered_bootstraps: int = 0
    discovered_manifests: int = 0
    attempted_scopes: int = 0
    spooled: int = 0
    uploaded: int = 0
    skipped_scopes: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    status: Mapping[str, Any] = field(default_factory=dict)

    @property
    def should_retry(self) -> bool:
        return self.disposition in {RunDisposition.RETRY, RunDisposition.BUSY}


class CredentialProvider(Protocol):
    def get_secret(self, identity_id: str) -> str: ...


class EnvironmentCredentialProvider:
    """Testing/console provider; production should use Credential Manager."""

    def __init__(self, *, variable: str = "COLLECTOR_MACHINE_SECRET", environ: Mapping[str, str] | None = None) -> None:
        self.variable = variable
        self.environ = os.environ if environ is None else environ

    def get_secret(self, identity_id: str) -> str:
        del identity_id
        value = str(self.environ.get(self.variable, ""))
        if not value:
            raise ValueError("collector credential is unavailable")
        return value


class WindowsCredentialProvider:
    """Read one secret from Windows Credential Manager without persisting it.

    ``keyring`` is optional at import time so offline Linux tests can use a
    fake provider; the installed Windows package supplies the backend.
    """

    def __init__(self, *, target_prefix: str = "StatsPlus/Collector/") -> None:
        self.target_prefix = target_prefix

    def get_secret(self, identity_id: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]
            secret = keyring.get_password(self.target_prefix + identity_id, "machine")
        except ImportError as error:
            raise ValueError("Windows Credential Manager support is not installed") from error
        if not secret:
            raise ValueError("collector credential is unavailable")
        return secret


def _now(clock: Any) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("collector clock must return datetime")
    if value.tzinfo is None:
        raise ValueError("collector clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _authorized_work(manifest: Mapping[str, Any], scopes: list[Any]) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Read only server-authored descriptors bound to an authorized base scope."""

    allowed = {str(value).strip() for value in scopes if str(value).strip()}
    raw = manifest.get("scope_descriptors")
    if raw is None:
        return tuple((scope, {"window": "season", "subject": "player"}) for scope in sorted(allowed))
    if not isinstance(raw, list):
        raise ProviderContractError("malformed_manifest")
    result: list[tuple[str, Mapping[str, Any]]] = []
    for descriptor in raw:
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"scope", "parameters"}:
            raise ProviderContractError("malformed_manifest")
        scope = str(descriptor.get("scope") or "").strip()
        parameters = descriptor.get("parameters")
        if scope not in allowed or not isinstance(parameters, Mapping):
            raise ProviderContractError("manifest_scope_mismatch")
        result.append((scope, dict(parameters)))
    return tuple(result)


def _instruction_expiry(instruction: Mapping[str, Any], *, now: datetime) -> bool:
    raw = instruction.get("expires_at") or instruction.get("collect_before")
    if not raw:
        return False
    try:
        return parse_datetime(str(raw)) <= now
    except (ValueError, ProviderContractError):
        raise ProviderContractError("malformed_control_instruction")


class ResidentialCollector:
    """Execute one bounded pull invocation.

    The class owns no long-running scheduler and never accepts a database URL.
    ``OutboxRepository`` is the only persistence dependency.
    """

    SUPPORTED_SCOPES = frozenset({
        "event_catalog", "athlete_catalog", "synergy", "synergy_play_types",
        "grouped_shot_types", "shot_types", "player_shot_types", "exact_shot_zones",
        "shot_zones", "player_shot_zones", "synergy:l15", "synergy_l15",
    })

    def __init__(
        self,
        *,
        client: RailwayClient,
        outbox: OutboxRepository,
        provider: Any,
        identity_id: str,
        environment: str,
        credential_provider: CredentialProvider | None = None,
        secret: str | None = None,
        release_version: str = "0.1.0",
        token_ttl_seconds: int = 300,
        poll_limit: int = 100,
        clock: Any | None = None,
        logger: logging.Logger | None = None,
        status: SafeStatus | None = None,
        instruction_cache: InstructionCache | None = None,
        release_checksum: str | None = None,
    ) -> None:
        if not identity_id.strip() or not environment.strip():
            raise ValueError("identity_id and environment are required")
        if credential_provider is None and not secret:
            raise ValueError("credential_provider or secret is required")
        self.client = client
        if client.environment != environment.strip():
            raise ValueError("Railway client and collector environments must match")
        self.outbox = outbox
        self.provider = provider
        self.identity_id = identity_id.strip()
        self.environment = environment.strip()
        self.credential_provider = credential_provider
        self._secret = secret
        self.release_version = release_version
        self.release_checksum = str(release_checksum or "").strip() or None
        self.token_ttl_seconds = int(token_ttl_seconds)
        self.poll_limit = max(1, min(int(poll_limit), 100))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.logger = logger or logging.getLogger("statsplus.residential")
        self.status = status or SafeStatus()
        self.instruction_cache = instruction_cache
        self.executor = ResidentialScopeExecutor(provider, clock=self.clock)

    def _secret_value(self) -> str:
        if self.credential_provider is not None:
            secret = self.credential_provider.get_secret(self.identity_id)
        else:
            secret = self._secret or ""
        if not isinstance(secret, str) or not secret:
            raise ValueError("collector credential is unavailable")
        return secret

    def _token(self) -> CollectorToken:
        return self.client.exchange_token(self._secret_value(), ttl_seconds=self.token_ttl_seconds)

    def _report_status(self, token: CollectorToken, *, state: str, reason: str) -> None:
        if not self.release_checksum:
            return
        try:
            self.client.report_status(
                token, state=state, reason=safe_code(reason),
                release_version=self.release_version, release_checksum=self.release_checksum,
            )
        except CollectorHTTPError:
            self.status.record("railway_unavailable")

    def _receipt_checksum(self, item: OutboxItem, receipt: Mapping[str, Any]) -> str:
        value = receipt.get("checksum")
        if not isinstance(value, str) or not value:
            raise OutboxError("Railway returned no durable receipt checksum")
        return value

    def _drain(self, token: CollectorToken) -> tuple[int, bool, tuple[str, ...]]:
        uploaded = 0
        retry = False
        failures: list[str] = []
        aged = self.outbox.aged_pending(now=_now(self.clock), limit=1000)
        seen = {item.item_id for item in aged}
        items = aged + tuple(item for item in self.outbox.pending(limit=1000) if item.item_id not in seen)
        for item in items:
            try:
                self.outbox.mark_attempt(item.item_id)
                receipt = (
                    self.client.upload_catalog(token, item.request_id or str(item.metadata.get("request_id") or ""), item.payload)
                    if item.kind == "catalog"
                    else self.client.upload_observation(token, item.payload)
                )
                checksum = self._receipt_checksum(item, receipt)
                self.outbox.acknowledge(item.item_id, checksum=checksum)
                uploaded += 1
                self.status.record("work_complete", scope=str(item.metadata.get("observation_type") or item.kind))
            except CollectorHTTPError as error:
                code = safe_code(error.reason, fallback="railway_unavailable")
                self.status.record(code, scope=str(item.metadata.get("observation_type") or item.kind))
                failures.append(code)
                if error.retryable:
                    retry = True
                    continue
                continue
            except OutboxError:
                failures.append("outbox_receipt_invalid")
                self.status.record("control_rejected", scope=str(item.metadata.get("observation_type") or item.kind))
                continue
        return uploaded, retry, tuple(failures)

    def _make_observation_envelopes(
        self, observations: tuple[Any, ...], *, manifest_id: str,
        instruction_id: str,
    ) -> tuple[ObservationEnvelope, ...]:
        retrieved_at = _now(self.clock)
        result: list[ObservationEnvelope] = []
        for observation in observations:
            if not getattr(observation, "complete", False):
                raise ProviderContractError("incomplete_observation")
            result.append(ObservationEnvelope.from_observation(
                observation, manifest_id=manifest_id, environment=self.environment,
                collector_id=self.identity_id, instruction_id=instruction_id,
                retrieved_at=retrieved_at,
            ))
        return tuple(result)

    def _process_bootstrap(self, request: Mapping[str, Any], *, catalog_type: str) -> tuple[int, tuple[str, ...]]:
        if request.get("status") not in {None, "pending"}:
            return 0, ("bootstrap_not_pending",)
        if _instruction_expiry(request, now=_now(self.clock)):
            self.status.record("work_pending", scope=f"{catalog_type}_catalog")
            return 0, ("bootstrap_expired",)
        catalog, observation_type = self.executor.execute_catalog(
            request, catalog_type=catalog_type, collector_id=self.identity_id,
            environment=self.environment, retrieved_at=_now(self.clock),
            catalog_version=self.release_version,
        )
        assert isinstance(catalog, CatalogEnvelope)
        self.outbox.enqueue_catalog(catalog)
        self.status.record("work_pending", scope=observation_type)
        return 1, ()

    def _process_manifest(self, manifest: Mapping[str, Any]) -> tuple[int, int, tuple[str, ...], tuple[str, ...], bool]:
        manifest_id = str(manifest.get("manifest_id") or "").strip()
        season = str(manifest.get("season") or "").strip()
        cutoff = str(manifest.get("cutoff") or "").strip()
        collect_before = manifest.get("collect_before")
        if not manifest_id or not season or not cutoff or not collect_before:
            raise ProviderContractError("malformed_manifest")
        if manifest.get("status") not in {None, "active"}:
            return 0, 0, ("manifest_not_active",), (), False
        now = _now(self.clock)
        if parse_datetime(str(collect_before)) <= now:
            return 0, 0, ("manifest_expired",), (), False
        accepted = manifest.get("accepted_versions", [2])
        if not isinstance(accepted, (list, tuple)) or not any(int(value) == 2 for value in accepted if str(value).isdigit()):
            raise ProviderContractError("schema_unsupported")
        scopes = manifest.get("scopes", [])
        if not isinstance(scopes, list):
            raise ProviderContractError("malformed_manifest")
        attempted = 0
        spooled = 0
        skipped: list[str] = []
        failures: list[str] = []
        transient = False
        for scope, parameters in _authorized_work(manifest, scopes):
            if not scope:
                raise ProviderContractError("malformed_manifest")
            if scope not in self.SUPPORTED_SCOPES:
                skipped.append(scope)
                continue
            if scope in {"synergy:l15", "synergy_l15"}:
                skipped.append(scope)
                self.status.record("provider_window_unsupported", scope=scope)
                continue
            attempted += 1
            work = ScopeWork(
                scope=scope, observation_type=scope, season=season, cutoff=cutoff,
                instruction_id=manifest_id, manifest_id=manifest_id,
                parameters=parameters,
            )
            try:
                for observation in self.executor.iter_scope(
                    work, collector_id=self.identity_id, environment=self.environment,
                    retrieved_at=_now(self.clock),
                ):
                    envelope = self._make_observation_envelopes(
                        (observation,), manifest_id=manifest_id, instruction_id=manifest_id,
                    )[0]
                    self.outbox.enqueue_observation(envelope)
                    spooled += 1
                    self.status.record("work_pending", scope=scope)
            except ProviderTransientError as error:
                transient = True
                failures.append(safe_code(str(error), fallback="provider_unavailable"))
            except (ProviderContractError, OutboxFull) as error:
                code = "outbox_full" if isinstance(error, OutboxFull) else getattr(error, "reason", str(error))
                failures.append(safe_code(code, fallback="provider_schema_changed"))
            except Exception as error:
                failures.append(safe_code(type(error).__name__, fallback="provider_failure"))
        return attempted, spooled, tuple(skipped), tuple(failures), transient

    def run(self) -> RunResult:
        """Run once and return a Task-Scheduler-friendly control outcome."""

        try:
            lease_context = self.outbox.process_lease(ttl_seconds=RUN_LEASE_SECONDS)
            lease_context.__enter__()
        except OutboxBusy:
            self.status.record("collector_busy")
            return self._result(RunDisposition.BUSY, failures=("collector_busy",))
        try:
            token: CollectorToken | None = None
            failures: tuple[str, ...] = ()
            token_failure: str | None = None
            try:
                token = self._token()
                self._report_status(token, state="running", reason="work_pending")
            except CollectorHTTPError as error:
                token_failure = safe_code(error.reason, fallback="token_failure")
                if not error.retryable:
                    return self._result(RunDisposition.NON_RETRYABLE, failures=(token_failure,))
                self.status.record(token_failure)
            except (ValueError, TypeError):
                return self._result(RunDisposition.NON_RETRYABLE, failures=("credential_unavailable",))

            uploaded = 0
            initial_transient = False
            if token is not None:
                uploaded, retry, failures = self._drain(token)
                if failures and not retry:
                    return self._result(RunDisposition.NON_RETRYABLE, uploaded=uploaded, failures=failures)
                initial_transient = retry

            discovery: Mapping[str, Any] | None = None
            if token is not None:
                try:
                    discovery = self.client.discover(token, limit=self.poll_limit)
                    if self.instruction_cache is not None:
                        try:
                            self.instruction_cache.store(discovery)
                        except (OSError, TypeError, ValueError):
                            self.status.record("cache_rejected")
                except CollectorHTTPError as error:
                    code = safe_code(error.reason, fallback="discovery_failure")
                    if not error.retryable:
                        return self._result(RunDisposition.NON_RETRYABLE, uploaded=uploaded, failures=failures + (code,))
                    failures += (code,)
            if discovery is None and self.instruction_cache is not None:
                cached = self.instruction_cache.load(now=_now(self.clock), environment=self.environment)
                discovery = {
                    "environment": self.environment,
                    "bootstrap_requests": list(cached.bootstrap_requests),
                    "manifests": list(cached.manifests),
                }
            if discovery is None:
                return self._result(RunDisposition.RETRY, uploaded=uploaded, failures=failures + ((token_failure,) if token_failure else ("discovery_failure",)))
            bootstraps = discovery.get("bootstrap_requests", [])
            manifests = discovery.get("manifests", [])
            if not isinstance(bootstraps, list) or not isinstance(manifests, list):
                return self._result(RunDisposition.NON_RETRYABLE, uploaded=uploaded, failures=("malformed_discovery",))
            attempted = spooled = 0
            skipped: list[str] = []
            local_failures: list[str] = []
            transient = initial_transient or token is None or bool(token_failure) or bool(failures)
            for request in bootstraps:
                if not isinstance(request, Mapping):
                    local_failures.append("malformed_bootstrap")
                    continue
                catalog_type = str(request.get("catalog_type") or "").strip()
                try:
                    count, request_failures = self._process_bootstrap(request, catalog_type=catalog_type)
                    spooled += count
                    skipped.extend(request_failures)
                except ProviderTransientError as error:
                    transient = True
                    local_failures.append(safe_code(str(error), fallback="provider_unavailable"))
                except (ProviderContractError, OutboxFull) as error:
                    code = "outbox_full" if isinstance(error, OutboxFull) else getattr(error, "reason", str(error))
                    local_failures.append(safe_code(code, fallback="provider_schema_changed"))
                except Exception as error:
                    local_failures.append(safe_code(type(error).__name__, fallback="provider_failure"))
            for manifest in manifests:
                if not isinstance(manifest, Mapping):
                    local_failures.append("malformed_manifest")
                    continue
                try:
                    count, produced, manifest_skipped, manifest_failures, manifest_transient = self._process_manifest(manifest)
                    attempted += count
                    spooled += produced
                    skipped.extend(manifest_skipped)
                    local_failures.extend(manifest_failures)
                    transient = transient or manifest_transient
                except ProviderTransientError as error:
                    transient = True
                    local_failures.append(safe_code(str(error), fallback="provider_unavailable"))
                except (ProviderContractError, OutboxFull) as error:
                    code = "outbox_full" if isinstance(error, OutboxFull) else getattr(error, "reason", str(error))
                    local_failures.append(safe_code(code, fallback="provider_schema_changed"))
                except Exception as error:
                    local_failures.append(safe_code(type(error).__name__, fallback="provider_failure"))

            if token is not None:
                final_uploaded, final_retry, final_failures = self._drain(token)
                uploaded += final_uploaded
                local_failures.extend(final_failures)
                transient = transient or final_retry
            elif self.outbox.count():
                transient = True
            try:
                self.outbox.enforce_retention(now=_now(self.clock))
            except OutboxRetentionError:
                local_failures.append("outbox_retention")
                self.status.record("outbox_retention")
            if transient:
                disposition = RunDisposition.RETRY
            elif local_failures and any(code not in {"bootstrap_expired", "manifest_expired", "provider_window_unsupported"} for code in local_failures):
                disposition = RunDisposition.NON_RETRYABLE
            elif not bootstraps and not manifests and not self.outbox.count() and not uploaded and not spooled:
                disposition = RunDisposition.NO_WORK
                self.status.record("no_work")
            else:
                disposition = RunDisposition.COMPLETE
                self.status.record("work_complete")
            result = self._result(
                disposition, bootstraps=len(bootstraps), manifests=len(manifests),
                attempted=attempted, spooled=spooled, uploaded=uploaded,
                skipped=tuple(skipped), failures=tuple(local_failures),
            )
            if token is not None:
                state = "pending" if disposition is RunDisposition.RETRY else disposition.value
                self._report_status(token, state=state, reason=(local_failures[0] if local_failures else disposition.value))
            return result
        finally:
            lease_context.__exit__(None, None, None)

    def _result(self, disposition: RunDisposition, *, bootstraps: int = 0, manifests: int = 0,
                attempted: int = 0, spooled: int = 0, uploaded: int = 0,
                skipped: tuple[str, ...] = (), failures: tuple[str, ...] = ()) -> RunResult:
        exit_code = {
            RunDisposition.NO_WORK: EXIT_NO_WORK,
            RunDisposition.COMPLETE: EXIT_NO_WORK,
            RunDisposition.RETRY: EXIT_RETRY,
            RunDisposition.NON_RETRYABLE: EXIT_NON_RETRYABLE,
            RunDisposition.BUSY: EXIT_BUSY,
        }[disposition]
        log_status(self.logger, disposition.value)
        return RunResult(
            disposition=disposition, exit_code=exit_code,
            discovered_bootstraps=bootstraps, discovered_manifests=manifests,
            attempted_scopes=attempted, spooled=spooled, uploaded=uploaded,
            skipped_scopes=skipped, failures=failures,
            status=self.status.snapshot(version=self.release_version, release_checksum=self.release_checksum),
        )


__all__ = [
    "EXIT_BUSY", "EXIT_NO_WORK", "EXIT_NON_RETRYABLE", "EXIT_RETRY", "RUN_LEASE_SECONDS",
    "CredentialProvider", "EnvironmentCredentialProvider", "RunDisposition",
    "RunResult", "ResidentialCollector", "WindowsCredentialProvider",
]
