"""Low-level async HTTP client for the Alarm.com JSON:API.

Wraps :class:`aiohttp.ClientSession` with ADC-specific headers, the
anti-forgery ``afg`` cookie, and uniform error handling.  All higher-level
code should go through this client rather than using aiohttp directly.
"""

from __future__ import annotations

__all__ = [
    "AdcClient",
]

import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from pyadc.const import URL_BASE
from pyadc.exceptions import (
    AuthenticationFailed,
    NotAuthorized,
    RequestBlocked,
    ServiceUnavailable,
    SessionExpired,
    UnexpectedResponse,
)

log = logging.getLogger(__name__)

# Customer JSON:API namespaces this client is permitted to call. Requests to any
# other first path segment under ``/web/api/`` — or to dealer/admin/central-
# station surfaces — are refused before they are sent. Defense in depth: the
# fixed ``/web/api/`` base already scopes to the customer API, but this stops a
# stray or malicious relative path from reaching somewhere it shouldn't.
# See plan.md Part 3 (safe-API policy).
_ALLOWED_API_NAMESPACES = frozenset(
    {
        "devices",       # partitions, sensors, locks, lights, thermostats, covers, valves, ...
        "video",         # cameras, snapshots, liveVideoSources, smrfImages
        "imageSensor",   # imageSensor/imageSensors device endpoints (peek-in)
        "systems",       # systems/systems
        "websockets",    # realtime token
        "identities",    # login identity / session properties
        "profile",       # profile/profile
        "engines",       # engines/twoFactorAuthentication/...
    }
)

_STATIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    # Device endpoints use NJsonApi and return JSON:API format for this Accept value.
    # Non-device endpoints (e.g. websocket token) override this per-call.
    "Accept": "application/vnd.api+json",
    "Accept-Language": "en-US,en;q=0.9",
}

# Kept for backwards-compatibility with any code that imported STANDARD_HEADERS directly.
STANDARD_HEADERS = {**_STATIC_HEADERS, "Referrer": f"{URL_BASE}/web/system/home"}


class AdcClient:
    """Async REST client for the Alarm.com JSON:API.

    Maintains the ``afg`` (anti-forgery) token extracted from response
    cookies and injects it as the ``AjaxRequestUniqueKey`` request header
    required by every authenticated API call.

    Attributes:
        _afg_token: Current anti-forgery token string.  Updated automatically
            on every response.
    """

    def __init__(self, session: aiohttp.ClientSession, base_url: str = URL_BASE) -> None:
        """Create an AdcClient.

        Args:
            session: Shared :class:`aiohttp.ClientSession`.  The caller is
                responsible for closing it when done.
            base_url: Root URL of the Alarm.com deployment, e.g.
                ``"https://www.alarm.com"``.  Defaults to the production
                endpoint.  Change this only via HA's advanced config option.
        """
        self._session = session
        self._afg_token: str = ""
        self._mfa_cookie: str = ""  # twoFactorAuthenticationId — injected into every request
        self._base_url: str = base_url.rstrip("/")
        self._api_url_base: str = f"{base_url}/web/api/"
        self._referrer: str = f"{base_url}/web/system/home"

        # Host allowlist: only ever send our authenticated session cookies to the
        # configured Alarm.com host and its subdomains (snapshots and relays live
        # on ``*.alarm.com``). Prevents a tampered API response from redirecting
        # credentialed requests to an attacker-controlled host. See plan.md Part 3.
        base_host = (urlparse(base_url).hostname or "").lower()
        self._base_host: str = base_host
        parts = base_host.split(".")
        self._root_domain: str = ".".join(parts[-2:]) if len(parts) >= 2 else base_host

    @property
    def session(self) -> aiohttp.ClientSession:
        """The underlying aiohttp session (needed by JanusSession)."""
        return self._session

    @property
    def base_url(self) -> str:
        """Root deployment URL (e.g. ``https://www.alarm.com``), no trailing slash.

        Used to resolve relative, authenticated resource paths (such as image
        viewer URLs) into absolute URLs for :meth:`fetch_bytes`.
        """
        return self._base_url

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build the full set of request headers.

        Args:
            extra: Optional additional headers to merge in (take precedence).

        Returns:
            A new headers dict with standard headers, the current AFG token,
            and any *extra* overrides applied.
        """
        headers = {**_STATIC_HEADERS, "Referrer": self._referrer}
        if self._afg_token:
            headers["AjaxRequestUniqueKey"] = self._afg_token
        if extra:
            headers.update(extra)
        return headers

    def _update_afg_from_response(self, response: aiohttp.ClientResponse) -> None:
        """Extract AFG anti-forgery token and MFA cookie from response cookies."""
        cookies = response.cookies
        if afg := cookies.get("afg"):
            self._afg_token = afg.value
        # Check response Set-Cookie header first
        mfa_value = cookies.get("twoFactorAuthenticationId")
        if mfa_value and mfa_value.value:
            if mfa_value.value != self._mfa_cookie:
                log.debug("Updated MFA cookie from response Set-Cookie header")
                self._mfa_cookie = mfa_value.value
        else:
            # Fallback: aiohttp stores domain-scoped cookies in the jar even when
            # they don't appear on response.cookies — check jar against response URL
            try:
                jar_cookies = self._session.cookie_jar.filter_cookies(response.url)
                jar_mfa = jar_cookies.get("twoFactorAuthenticationId")
                if jar_mfa and jar_mfa.value and jar_mfa.value != self._mfa_cookie:
                    log.debug("Updated MFA cookie from session cookie jar")
                    self._mfa_cookie = jar_mfa.value
            except Exception as exc:
                log.debug("Failed to read MFA cookie from session jar: %s", exc)

    def _mfa_cookies(self) -> dict[str, str] | None:
        """Return the MFA cookie dict to inject, or None if not set."""
        return {"twoFactorAuthenticationId": self._mfa_cookie} if self._mfa_cookie else None

    def _host_allowed(self, host: str) -> bool:
        """True if *host* is the configured Alarm.com host or a subdomain of it."""
        host = (host or "").lower()
        if not host:
            return False
        return (
            host == self._base_host
            or host == self._root_domain
            or host.endswith("." + self._root_domain)
        )

    def _guard_request(self, url: str, path: str, effective_base: str) -> None:
        """Enforce the client's safety policy before a request leaves the process.

        1. The target host must be Alarm.com (or a subdomain) — never leak
           session cookies to a foreign host.
        2. When calling the default customer API base with a relative path, the
           path must live in an approved customer namespace and must not attempt
           path traversal.

        Raises:
            RequestBlocked: if either rule is violated. Not a ``NotAuthorized``
            subclass, so the re-login/retry path will not try to recover.
        """
        host = urlparse(url).hostname or ""
        if not self._host_allowed(host):
            raise RequestBlocked(f"Refusing request to non-Alarm.com host {host!r}")

        # Namespace check only applies to relative paths on the customer API base.
        if path.startswith("http") or effective_base != self._api_url_base:
            return
        if path.startswith("/") or path.startswith("\\") or ".." in path:
            raise RequestBlocked(f"Refusing unsafe API path {path!r}")
        first_segment = path.split("?", 1)[0].split("/", 1)[0]
        if first_segment not in _ALLOWED_API_NAMESPACES:
            raise RequestBlocked(
                f"Refusing non-customer API namespace {first_segment!r} (path={path!r})"
            )

    async def get(
        self,
        path: str,
        *,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated GET request.

        Args:
            path: Relative API path (e.g. ``"devices/partition"``) or a full
                URL (detected by ``http`` prefix).
            base_url: Base URL prepended when *path* is relative.  Defaults
                to the instance's configured API base URL.
            extra_headers: Optional additional headers for this request only.

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            AuthenticationFailed: HTTP 401.
            NotAuthorized: HTTP 403.
            ServiceUnavailable: HTTP 5xx.
            UnexpectedResponse: Any other non-200 status.
        """
        effective_base = base_url if base_url is not None else self._api_url_base
        url = f"{effective_base}{path}" if not path.startswith("http") else path
        self._guard_request(url, path, effective_base)
        async with self._session.get(url, headers=self._build_headers(extra_headers), cookies=self._mfa_cookies()) as resp:
            self._update_afg_from_response(resp)
            await self._check_response(resp)
            return await resp.json(content_type=None)

    async def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated POST request.

        Args:
            path: Relative API path or full URL.
            body: JSON-serialisable request body.  Defaults to ``{}``.
            base_url: Base URL prepended when *path* is relative.  Defaults
                to the instance's configured API base URL.
            extra_headers: Optional additional headers for this request only.

        Returns:
            Parsed JSON response body, or ``{}`` if the response body is empty
            or non-JSON (e.g. 204 No Content action endpoints).

        Raises:
            AuthenticationFailed: HTTP 401.
            NotAuthorized: HTTP 403.
            ServiceUnavailable: HTTP 5xx.
            UnexpectedResponse: Any other non-200 status.
        """
        effective_base = base_url if base_url is not None else self._api_url_base
        url = f"{effective_base}{path}" if not path.startswith("http") else path
        self._guard_request(url, path, effective_base)
        headers = self._build_headers(extra_headers)
        log.debug("POST %s  AFG=%s", url, bool(self._afg_token))
        async with self._session.post(
            url,
            json=body or {},
            headers=headers,
            cookies=self._mfa_cookies(),
        ) as resp:
            self._update_afg_from_response(resp)
            await self._check_response(resp)
            try:
                return await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as exc:
                log.debug("Failed to parse POST response as JSON: %s", exc)
                return {}

    async def put(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated PUT request.

        Args:
            path: Relative API path or full URL.
            body: JSON-serialisable request body.  Defaults to ``{}``.
            base_url: Base URL prepended when *path* is relative.
            extra_headers: Optional additional headers for this request only.

        Returns:
            Parsed JSON response body, or ``{}`` if the response body is empty
            or non-JSON.
        """
        effective_base = base_url if base_url is not None else self._api_url_base
        url = f"{effective_base}{path}" if not path.startswith("http") else path
        self._guard_request(url, path, effective_base)
        headers = self._build_headers(extra_headers)
        log.debug("PUT %s  AFG=%s", url, bool(self._afg_token))
        async with self._session.put(
            url,
            json=body or {},
            headers=headers,
            cookies=self._mfa_cookies(),
        ) as resp:
            self._update_afg_from_response(resp)
            await self._check_response(resp)
            try:
                return await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as exc:
                log.debug("Failed to parse PUT response as JSON: %s", exc)
                return {}

    async def fetch_bytes(self, url: str) -> bytes:
        """Fetch raw bytes from a URL using the current session credentials.

        Useful for downloading binary resources (e.g. camera snapshots) that
        require authenticated headers but return non-JSON content.

        Args:
            url: Absolute URL to fetch.

        Returns:
            Raw response body as bytes.

        Raises:
            AuthenticationFailed: HTTP 401.
            NotAuthorized: HTTP 403.
            ServiceUnavailable: HTTP 5xx.
            UnexpectedResponse: Any other non-200 status.
        """
        # fetch_bytes only ever receives full URLs returned by the API (e.g. signed
        # snapshot URLs). Enforce the host allowlist so credentialed reads can't be
        # redirected off the Alarm.com domain.
        self._guard_request(url, url, self._api_url_base)
        async with self._session.get(
            url, headers=self._build_headers(), cookies=self._mfa_cookies()
        ) as resp:
            self._update_afg_from_response(resp)
            await self._check_response(resp)
            return await resp.read()

    async def _check_response(self, resp: aiohttp.ClientResponse) -> None:
        """Raise appropriate exception for error HTTP status codes."""
        if resp.status in (200, 201, 204):
            return
        if resp.status == 403:
            raise NotAuthorized(f"403 Forbidden: {resp.url}")
        if resp.status == 401:
            raise AuthenticationFailed(f"401 Unauthorized: {resp.url}")
        if resp.status >= 500:
            raise ServiceUnavailable(f"{resp.status} Server Error: {resp.url}")
        if resp.status == 404:
            raise UnexpectedResponse(f"404 Not Found: {resp.url}")
        # Check for ADC-specific session expiry patterns in 422/other
        raise UnexpectedResponse(f"Unexpected status {resp.status}: {resp.url}")
