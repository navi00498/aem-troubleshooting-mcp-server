"""Minimal, read-only HTTP client for AEM. Only GET requests are ever sent."""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_RESPONSE_BYTES = 8_000_000
_FALSE = {"0", "false", "no", "off"}


class AemError(Exception):
    """A request to AEM failed. The message is safe to show to the user (it never contains credentials)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AemConfig:
    url: str = "http://localhost:4502"
    user: str | None = None
    password: str | None = None
    token: str | None = None
    timeout: float = 20.0
    verify_tls: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AemConfig:
        env = os.environ if env is None else env
        return cls(
            url=env.get("AEM_URL", cls.url).rstrip("/"),
            user=env.get("AEM_USER") or None,
            password=env.get("AEM_PASSWORD") or None,
            token=env.get("AEM_TOKEN") or None,
            timeout=float(env.get("AEM_TIMEOUT", cls.timeout)),
            verify_tls=env.get("AEM_VERIFY_TLS", "true").strip().lower() not in _FALSE,
        )

    def describe(self) -> dict[str, Any]:
        """Connection details without secrets."""
        auth = "bearer token" if self.token else "basic" if self.user else "none"
        return {"url": self.url, "auth": auth, "user": self.user, "verify_tls": self.verify_tls}


def _describe_http_error(status: int, path: str) -> str:
    if status == 401:
        return "AEM returned 401 Unauthorized. Set AEM_USER/AEM_PASSWORD or AEM_TOKEN for an account that can read it."
    if status == 403:
        return (
            f"AEM returned 403 Forbidden for {path}. The user lacks permission, or the endpoint is blocked "
            "(for example /system/console is not exposed on AEM as a Cloud Service environments or behind Dispatcher)."
        )
    if status == 404:
        return f"AEM returned 404 Not Found for {path}. The resource or console plugin doesn't exist on this instance."
    return f"AEM returned HTTP {status} for {path}."


def _looks_like_html(data: bytes) -> bool:
    head = data[:200].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


class AemClient:
    def __init__(self, config: AemConfig) -> None:
        self.config = config
        if config.verify_tls:
            self._ssl = ssl.create_default_context()
        else:
            self._ssl = ssl._create_unverified_context()  # noqa: S323 - opt-in for local SDKs with self-signed certs

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "aem-troubleshooting-mcp"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        elif self.config.user:
            raw = f"{self.config.user}:{self.config.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    def url_for(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        url = self.config.url + urllib.parse.quote(path, safe="/:.-_~")
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return url

    def get(self, path: str, params: Mapping[str, Any] | None = None, accept: str = "*/*") -> bytes:
        request = urllib.request.Request(self.url_for(path, params), headers=self._headers(accept), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout, context=self._ssl) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise AemError(_describe_http_error(exc.code, path), exc.code) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise AemError(f"Timed out after {self.config.timeout:g}s waiting for {self.config.url}.") from None
            raise AemError(f"Cannot reach AEM at {self.config.url}: {exc.reason}. Is the instance running?") from None
        except TimeoutError:
            raise AemError(f"Timed out after {self.config.timeout:g}s waiting for {self.config.url}.") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise AemError(f"Response from {path} is larger than {MAX_RESPONSE_BYTES:,} bytes; narrow the request.")
        if _looks_like_html(data):
            raise AemError(
                f"AEM returned an HTML page for {path} instead of data. This is usually the login page: "
                "check AEM_USER/AEM_PASSWORD or AEM_TOKEN."
            )
        return data

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        data = self.get(path, params, accept="application/json")
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise AemError(f"AEM returned invalid JSON for {path}.") from None

    def get_text(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        return self.get(path, params, accept="text/plain").decode("utf-8", errors="replace")
