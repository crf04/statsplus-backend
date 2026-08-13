"""Minimal authenticated HTTP client for the Railway collector boundary."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urlencode, urljoin, urlsplit

import requests


class CollectorHTTPError(RuntimeError):
    """A bounded HTTP/control outcome safe for status diagnostics."""

    def __init__(self, reason: str, *, status: int | None = None, retry_after: int | None = None, retryable: bool = False) -> None:
        self.reason = str(reason)
        self.status = status
        self.retry_after_seconds = retry_after
        self.retryable = bool(retryable)
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: bytes | str | Mapping[str, Any] | None = None
    headers: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.headers is None:
            object.__setattr__(self, "headers", {})

    def json(self) -> Any:
        if isinstance(self.body, Mapping):
            return dict(self.body)
        if self.body is None or self.body == b"" or self.body == "":
            return {}
        value = self.body.decode("utf-8") if isinstance(self.body, bytes) else self.body
        try:
            return json.loads(value)
        except (TypeError, ValueError) as error:
            raise CollectorHTTPError("malformed_control_response", status=self.status) from error


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, json_body: Mapping[str, Any] | None = None,
                timeout: float = 30.0) -> HTTPResponse: ...


class RequestsTransport:
    """Requests adapter with no application or Flask dependency."""

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, json_body: Mapping[str, Any] | None = None,
                timeout: float = 30.0) -> HTTPResponse:
        try:
            response = self.session.request(
                method.upper(), url, headers=dict(headers or {}), data=body,
                json=dict(json_body) if json_body is not None else None,
                timeout=max(0.1, float(timeout)),
            )
        except requests.exceptions.Timeout as error:
            raise CollectorHTTPError("railway_timeout", retryable=True) from error
        except requests.exceptions.RequestException as error:
            raise CollectorHTTPError("railway_unavailable", retryable=True) from error
        return HTTPResponse(response.status_code, response.content, dict(response.headers))


@dataclass(frozen=True, slots=True)
class CollectorToken:
    token: str
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    @classmethod
    def from_response(cls, value: Any, *, now: datetime | None = None) -> "CollectorToken":
        current = now or datetime.now(timezone.utc)
        if isinstance(value, str):
            token = value.strip()
            expires = current + timedelta(minutes=5)
        elif isinstance(value, Mapping):
            token = str(value.get("token") or value.get("access_token") or "").strip()
            raw_expiry = value.get("expires_at") or value.get("expires_in")
            if isinstance(raw_expiry, (int, float)) and not isinstance(raw_expiry, bool):
                expires = current + timedelta(seconds=max(1, int(raw_expiry)))
            elif isinstance(raw_expiry, str):
                try:
                    expires = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                except ValueError as error:
                    raise CollectorHTTPError("malformed_token_response") from error
            else:
                expires = current + timedelta(minutes=5)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            expires = expires.astimezone(timezone.utc)
        else:
            raise CollectorHTTPError("malformed_token_response")
        if not token or len(token) > 16_384:
            raise CollectorHTTPError("malformed_token_response")
        return cls(token=token, expires_at=expires)


def _retry_after(headers: Mapping[str, str]) -> int | None:
    try:
        value = next((raw for key, raw in headers.items() if key.casefold() == "retry-after"), None)
        if value is None:
            return None
        return max(1, min(int(float(value)), 6 * 60 * 60))
    except (TypeError, ValueError, OverflowError):
        return None


def _reason(response: HTTPResponse, *, fallback: str) -> str:
    try:
        document = response.json()
        if isinstance(document, Mapping):
            nested = document.get("error")
            if isinstance(nested, Mapping):
                code = nested.get("code")
            else:
                code = document.get("code")
            if isinstance(code, str) and code.replace("_", "").isalnum():
                return code[:80]
    except CollectorHTTPError:
        pass
    return fallback


class RailwayClient:
    """Authenticated client that exposes only collector control operations."""

    def __init__(self, base_url: str, *, identity_id: str, environment: str,
                 transport: Transport | None = None, timeout: float = 30.0,
                 allow_insecure_localhost: bool = False, release_version: str | None = None,
                 clock: Any = time.time) -> None:
        normalized = str(base_url).strip().rstrip("/") + "/"
        try:
            endpoint = urlsplit(normalized)
            hostname = endpoint.hostname
            _ = endpoint.port
        except ValueError as error:
            raise ValueError("Railway endpoint is malformed") from error
        if endpoint.scheme not in {"https", "http"} or not hostname or endpoint.username or endpoint.password:
            raise ValueError("Railway endpoint must use HTTPS")
        if endpoint.scheme == "http" and not (
            allow_insecure_localhost and hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Railway endpoint must use HTTPS")
        if not identity_id.strip() or not environment.strip():
            raise ValueError("collector identity and environment are required")
        self.base_url = normalized
        self.identity_id = identity_id.strip()
        self.environment = environment.strip()
        self.transport = transport or RequestsTransport()
        self.timeout = max(0.1, float(timeout))
        self.release_version = str(release_version or "").strip()[:80]
        self.clock = clock

    def _request(self, method: str, path: str, *, token: CollectorToken | str | None = None,
                 json_body: Mapping[str, Any] | None = None, body: bytes | None = None,
                 headers: Mapping[str, str] | None = None) -> HTTPResponse:
        if not path.startswith("/"):
            path = "/" + path
        merged = {"Accept": "application/json", **dict(headers or {})}
        if self.release_version:
            merged.setdefault("X-Collector-Release", self.release_version)
        if token is not None:
            bearer = token.token if isinstance(token, CollectorToken) else str(token)
            if not bearer or (isinstance(token, CollectorToken) and token.is_expired):
                raise CollectorHTTPError("token_expired", status=401)
            merged["Authorization"] = f"Bearer {bearer}"
        try:
            response = self.transport.request(
                method, urljoin(self.base_url, path.lstrip("/")), headers=merged,
                json_body=json_body, body=body, timeout=self.timeout,
            )
        except CollectorHTTPError:
            raise
        except Exception as error:
            raise CollectorHTTPError("railway_unavailable", retryable=True) from error
        if response.status >= 200 and response.status < 300:
            return response
        retry_after = _retry_after(response.headers)
        if response.status == 401:
            raise CollectorHTTPError(_reason(response, fallback="invalid_token"), status=401)
        if response.status == 403:
            raise CollectorHTTPError(_reason(response, fallback="forbidden"), status=403)
        if response.status == 404:
            raise CollectorHTTPError(_reason(response, fallback="not_found"), status=404)
        if response.status == 409:
            raise CollectorHTTPError(_reason(response, fallback="operation_conflict"), status=409)
        if response.status in {408, 425, 429} or response.status >= 500:
            raise CollectorHTTPError(_reason(response, fallback="railway_unavailable"), status=response.status,
                                     retry_after=retry_after or 30, retryable=True)
        raise CollectorHTTPError(_reason(response, fallback="control_rejected"), status=response.status)

    def exchange_token(self, secret: str, *, scopes: list[str] | None = None,
                       providers: list[str] | None = None, surfaces: list[str] | None = None,
                       ttl_seconds: int = 300) -> CollectorToken:
        if not isinstance(secret, str) or not secret:
            raise ValueError("collector credential is required")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 900:
            raise ValueError("ttl_seconds must be an integer from 1 through 900")
        body: dict[str, Any] = {"identity_id": self.identity_id, "secret": secret, "ttl_seconds": ttl_seconds}
        if scopes is not None:
            body["scopes"] = list(scopes)
        if providers is not None:
            body["providers"] = list(providers)
        if surfaces is not None:
            body["surfaces"] = list(surfaces)
        response = self._request("POST", "/api/collector/token", json_body=body)
        return CollectorToken.from_response(response.json())

    def discover(self, token: CollectorToken | str, *, limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        response = self._request("GET", f"/api/collector/discovery?{urlencode({'limit': bounded})}", token=token)
        value = response.json()
        if not isinstance(value, Mapping):
            raise CollectorHTTPError("malformed_discovery")
        if str(value.get("environment", self.environment)) != self.environment:
            raise CollectorHTTPError("environment_mismatch", status=403)
        requests_value = value.get("bootstrap_requests", [])
        manifests = value.get("manifests", [])
        if not isinstance(requests_value, list) or not isinstance(manifests, list):
            raise CollectorHTTPError("malformed_discovery")
        return {"environment": self.environment, "bootstrap_requests": list(requests_value), "manifests": list(manifests)}

    def get_manifest(self, token: CollectorToken | str, manifest_id: str) -> dict[str, Any]:
        if not manifest_id.strip():
            raise ValueError("manifest_id is required")
        value = self._request("GET", f"/api/collector/manifest/{quote(manifest_id, safe='')}", token=token).json()
        if not isinstance(value, Mapping):
            raise CollectorHTTPError("malformed_manifest")
        return dict(value)

    def upload_observation(self, token: CollectorToken | str, compressed_wire: bytes) -> dict[str, Any]:
        response = self._request(
            "POST", "/api/collector/observations", token=token, body=compressed_wire,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        value = response.json()
        if not isinstance(value, Mapping):
            raise CollectorHTTPError("malformed_receipt")
        return dict(value)

    def upload_catalog(self, token: CollectorToken | str, request_id: str, compressed_wire: bytes) -> dict[str, Any]:
        if not request_id.strip():
            raise ValueError("request_id is required")
        response = self._request(
            "POST", f"/api/collector/catalog/{quote(request_id, safe='')}", token=token, body=compressed_wire,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        value = response.json()
        if not isinstance(value, Mapping):
            raise CollectorHTTPError("malformed_receipt")
        return dict(value)


__all__ = [
    "CollectorHTTPError", "CollectorToken", "HTTPResponse", "RailwayClient",
    "RequestsTransport", "Transport",
]
