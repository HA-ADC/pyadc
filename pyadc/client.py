"""Low-level async HTTP client for the Alarm.com JSON:API.

Wraps :class:`aiohttp.ClientSession` with ADC-specific headers, the
anti-forgery ``afg`` cookie, and uniform error handling.  All higher-level
code should go through this client rather than using aiohttp directly.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from pyadc.const import API_URL_BASE, URL_BASE
from pyadc.exceptions import (
    AuthenticationFailed,
    NotAuthorized,
    ServiceUnavailable,
    SessionExpired,
    UnexpectedResponse,
)

log = logging.getLogger(__name__)

STANDARD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    # Device endpoints use NJsonApi and return JSON:API format for this Accept value.
    # Non-device endpoints (e.g. websocket token) override this per-call.
    "Accept": "application/vnd.api+json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referrer": "https://www.alarm.com/web/system/home",
}


class AdcClient:
    """Async REST client for the Alarm.com JSON:API.

    Maintains the ``afg`` (anti-forgery) token extracted from response
    cookies and injects it as the ``AjaxRequestUniqueKey`` request header
    required by every authenticated API call.

    Attributes:
        _afg_token: Current anti-forgery token string.  Updated automatically
            on every response.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Create an AdcClient.

        Args:
            session: Shared :class:`aiohttp.ClientSession`.  The caller is
                responsible for closing it when done.
        """
        self._session = session
        self._afg_token: str = ""

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build the full set of request headers.

        Args:
            extra: Optional additional headers to merge in (take precedence).

        Returns:
            A new headers dict with standard headers, the current AFG token,
            and any *extra* overrides applied.
        """
        headers = {**STANDARD_HEADERS}
        if self._afg_token:
            headers["AjaxRequestUniqueKey"] = self._afg_token
        if extra:
            headers.update(extra)
        return headers

    def _update_afg_from_response(self, response: aiohttp.ClientResponse) -> None:
        """Extract AFG anti-forgery token from response cookies."""
        cookies = response.cookies
        if afg := cookies.get("afg"):
            self._afg_token = afg.value

    async def get(
        self,
        path: str,
        *,
        base_url: str = API_URL_BASE,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated GET request.

        Args:
            path: Relative API path (e.g. ``"devices/partition"``) or a full
                URL (detected by ``http`` prefix).
            base_url: Base URL prepended when *path* is relative.
            extra_headers: Optional additional headers for this request only.

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            AuthenticationFailed: HTTP 401.
            NotAuthorized: HTTP 403.
            ServiceUnavailable: HTTP 5xx.
            UnexpectedResponse: Any other non-200 status.
        """
        url = f"{base_url}{path}" if not path.startswith("http") else path
        async with self._session.get(url, headers=self._build_headers(extra_headers)) as resp:
            self._update_afg_from_response(resp)
            await self._check_response(resp)
            return await resp.json(content_type=None)

    async def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        base_url: str = API_URL_BASE,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated POST request.

        Args:
            path: Relative API path or full URL.
            body: JSON-serialisable request body.  Defaults to ``{}``.
            base_url: Base URL prepended when *path* is relative.
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
        url = f"{base_url}{path}" if not path.startswith("http") else path
        async with self._session.post(
            url,
            json=body or {},
            headers=self._build_headers(extra_headers),
        ) as resp:
            self._update_afg_from_response(resp)
            await self._check_response(resp)
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {}

    async def _check_response(self, resp: aiohttp.ClientResponse) -> None:
        """Raise appropriate exception for error HTTP status codes."""
        if resp.status == 200:
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
