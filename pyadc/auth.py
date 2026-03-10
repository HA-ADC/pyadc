"""Authentication controller for the pyadc library.

Implements the 4-step Alarm.com login flow (HTML scrape → credential POST →
user-data load → 2FA check) and the OTP verification helpers.  Also manages
the long-running keep-alive loop that prevents session expiry.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import aiohttp
from bs4 import BeautifulSoup

from pyadc.const import (
    API_URL_BASE,
    FORM_SUBMIT_URL,
    KEEP_ALIVE_INTERVAL_S,
    LOGIN_URL,
    OtpType,
    URL_BASE,
)
from pyadc.exceptions import (
    AuthenticationFailed,
    MustConfigureMfa,
    OtpRequired,
    ServiceUnavailable,
    UnexpectedResponse,
)
from pyadc.models.auth import TwoFactorAuthentication

if TYPE_CHECKING:
    from pyadc.client import AdcClient

log = logging.getLogger(__name__)

TWO_FACTOR_PATH = "engines/twoFactorAuthentication/twoFactorAuthentications"


class AuthController:
    """Handles Alarm.com authentication: login, OTP, keep-alive.

    The login flow has four steps:
    1. **Scrape** — GET the login page and harvest the hidden ASP.NET form
       fields (``__VIEWSTATE`` etc.).
    2. **Submit** — POST credentials to ``Default.aspx`` and capture the
       anti-forgery ``afg`` cookie.
    3. **Load** — Parallel GET of ``identities`` and ``profile/profile`` to
       populate ``_user_id`` and ``_keep_alive_url``.
    4. **2FA** — Check whether the current device is trusted; if not and OTP
       types are enabled, raise :exc:`~pyadc.exceptions.OtpRequired`.

    When :exc:`~pyadc.exceptions.OtpRequired` is raised the caller must:
    - Send a code via :meth:`send_otp_sms` or :meth:`send_otp_email`.
    - Verify it with :meth:`verify_otp` (returns the MFA cookie).
    - Optionally call :meth:`trust_device` to avoid future challenges.
    - Re-call :meth:`login` (with the stored ``mfa_cookie``) to complete.
    """

    def __init__(
        self,
        client: AdcClient,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        mfa_cookie: str = "",
    ) -> None:
        """Create the auth controller.

        Args:
            client: Shared :class:`~pyadc.client.AdcClient` instance.
            session: The raw :class:`aiohttp.ClientSession` used for the form
                POST (which must follow HTML redirects).
            username: Alarm.com account e-mail.
            password: Alarm.com account password.
            mfa_cookie: Pre-stored two-factor auth cookie.  When supplied and
                valid, step 4 of :meth:`login` will see the device as trusted
                and skip the OTP challenge.
        """
        self._client = client
        self._session = session
        self._username = username
        self._password = password
        self.mfa_cookie: str = mfa_cookie
        self._keep_alive_url: str = ""
        self._user_id: str = ""
        self._keep_alive_task: asyncio.Task | None = None
        self._form_fields: dict[str, str] = {}
        self._form_submit_url: str = FORM_SUBMIT_URL
        self._username_field: str | None = None
        self._password_field: str | None = None
        self._submit_field: str | None = None

    async def login(self) -> None:
        """Execute the full 4-step Alarm.com login flow.

        Steps:
        1. Scrape hidden ASP.NET form fields from the login page.
        2. POST credentials; detect failure from redirect URL parameters.
        3. Load identity/profile data in parallel to get user ID and
           keep-alive URL.
        4. Query 2FA status; raise :exc:`~pyadc.exceptions.OtpRequired` if
           the device is not trusted and OTP is required.

        Raises:
            AuthenticationFailed: On bad credentials or locked account.
            OtpRequired: When two-factor authentication is required.  The
                exception carries ``otp_types`` indicating which channels
                (app / SMS / email) are available.
            ServiceUnavailable: On 5xx responses from Alarm.com.
        """
        # Step 1: GET login page, scrape ASP.NET form fields
        await self._scrape_login_page()
        # Step 2: POST credentials
        await self._submit_credentials()
        # Step 3: Load identity/profile/dealer (parallel)
        await self._load_user_data()
        # Step 4: Check 2FA status; raise OtpRequired if needed
        await self._check_two_factor()

    async def _scrape_login_page(self) -> None:
        """GET login page and dynamically extract ALL form field names and values.

        Follows redirects so the final URL (which is the POST target) is captured.
        Field names are ASP.NET-generated and must be read from the rendered HTML —
        they cannot be hardcoded because they depend on the NamingContainer hierarchy.
        """
        async with self._session.get(LOGIN_URL, allow_redirects=True) as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", {"id": "form1"}) or soup.find("form")
        if not form:
            raise AuthenticationFailed("Could not find login form on page")

        # Extract ALL input fields — ASP.NET VIEWSTATE and visible inputs alike
        self._form_fields = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            self._form_fields[name] = inp.get("value", "")

        # Discover rendered field names dynamically (ends-with match on control ID)
        self._username_field = next(
            (k for k in self._form_fields if k.endswith("txtUsername") or k.endswith("txtUserName")),
            None,
        )
        self._password_field = next(
            (k for k in self._form_fields if k.endswith("txtPassword")),
            None,
        )
        self._submit_field = next(
            (k for k in self._form_fields if k.endswith("butLogin") or k.endswith("signInButton")),
            None,
        )

        # The signInButton uses ASP.NET cross-page postback targeting /Default.aspx.
        # CustomerDotNet is deployed under /web/, so the actual POST target is FORM_SUBMIT_URL.
        self._form_submit_url = FORM_SUBMIT_URL

        log.debug(
            "Login page scraped: username_field=%s, password_field=%s, submit_field=%s",
            self._username_field,
            self._password_field,
            self._submit_field,
        )

        if not self._username_field or not self._password_field:
            raise AuthenticationFailed(
                f"Could not locate username/password inputs on login page. "
                f"Fields found: {list(self._form_fields.keys())}"
            )

    async def _submit_credentials(self) -> None:
        """POST credentials to the login form. Detect failure by redirect URL."""
        data = dict(self._form_fields)
        data[self._username_field] = self._username
        data[self._password_field] = self._password
        if self._submit_field:
            data[self._submit_field] = "Login"
        # JavaScriptTest defaults to "0" in the hidden input; JavaScript normally sets
        # it to "1" in the browser. If it stays "0", the server rejects the login.
        if "JavaScriptTest" in data:
            data["JavaScriptTest"] = "1"
        # IsFromNewSite must be "1" to trigger MainProcessAuthentication (the normal login flow).
        # The server skips credential processing if this field is not "1".
        if "IsFromNewSite" in data:
            data["IsFromNewSite"] = "1"

        log.debug("Submitting login POST to %s", self._form_submit_url)
        async with self._session.post(
            self._form_submit_url,
            data=data,
            headers={"User-Agent": "Mozilla/5.0", "Referrer": LOGIN_URL},
            allow_redirects=False,
        ) as resp:
            location = resp.headers.get("Location", "")
            # Follow the redirect to complete the login sequence and accumulate cookies
            if resp.status in (301, 302, 303, 307, 308) and location:
                async with self._session.get(
                    location if location.startswith("http") else f"{URL_BASE}{location}",
                    allow_redirects=True,
                ) as final_resp:
                    final_url = str(final_resp.url)
                    body = await final_resp.text()
                    log.debug("Login redirect chain: final_url=%s", final_url)
            else:
                final_url = str(resp.url)
                body = await resp.text()
            if "m=login_fail" in final_url or "m=login_fail" in body:
                raise AuthenticationFailed("Invalid username or password")
            if "m=LockedOut" in final_url:
                raise AuthenticationFailed("Account is locked out")
            if "/login" in final_url and "/web/" not in final_url:
                log.warning("Login POST returned to login page — likely failed silently")
            # Extract AFG anti-forgery token from the response cookies
            afg = resp.cookies.get("afg") or self._session.cookie_jar.filter_cookies(
                resp.url
            ).get("afg")
            if afg:
                self._client._afg_token = afg.value
                log.debug("AFG token acquired after login")
            else:
                log.warning("No AFG token in cookies after login — session may not be established")

    async def _load_user_data(self) -> None:
        """Load identity, profile, and dealer data in parallel."""
        try:
            results = await asyncio.gather(
                self._client.get("identities"),
                self._client.get("profile/profile"),
                return_exceptions=True,
            )
        except Exception as err:
            log.warning("Failed to load user data: %s", err)
            return

        # Parse identity
        identity_resp = results[0]
        if not isinstance(identity_resp, Exception) and identity_resp.get("data"):
            data = identity_resp["data"]
            items = data if isinstance(data, list) else [data]
            if items:
                self._user_id = items[0].get("id", "")

        # Parse profile — extract keep_alive_url
        profile_resp = results[1]
        if not isinstance(profile_resp, Exception) and profile_resp.get("data"):
            attrs = profile_resp["data"].get("attributes", {})
            # camelCase: keepAliveUrl
            self._keep_alive_url = attrs.get("keepAliveUrl", attrs.get("keep_alive_url", ""))

    async def _check_two_factor(self) -> None:
        """Check 2FA status and raise OtpRequired if MFA is needed."""
        if not self._user_id:
            return
        try:
            resp = await self._client.get(f"{TWO_FACTOR_PATH}/{self._user_id}")
        except Exception:
            return

        data = resp.get("data", {})
        attrs = data.get("attributes", {}) if isinstance(data, dict) else {}

        # Check if current device is trusted
        trusted = attrs.get("isTrustedDevice", attrs.get("is_trusted_device", False))
        if trusted:
            return

        # Check available OTP types
        otp_type_mask = attrs.get("enabledTwoFactorTypes", attrs.get("enabled_two_factor_types", 0))
        if otp_type_mask:
            raise OtpRequired(otp_types=otp_type_mask)

    async def send_otp_sms(self) -> None:
        """Trigger Alarm.com to send an OTP code via SMS to the account's phone."""
        await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/sendTwoFactorAuthenticationCodeViaSms"
        )

    async def send_otp_email(self) -> None:
        """Trigger Alarm.com to send an OTP code via e-mail to the account address."""
        await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/sendTwoFactorAuthenticationCodeViaEmail"
        )

    async def verify_otp(self, code: str) -> str:
        """Submit the OTP code to complete the two-factor challenge.

        Args:
            code: The numeric code received via SMS or e-mail.

        Returns:
            The ``twoFactorAuthenticationId`` cookie value.  Store this as
            ``mfa_cookie`` to skip future OTP challenges on this device.
        """
        resp = await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/verifyTwoFactorCode",
            {"twoFactorAuthenticationCode": code},
        )
        # Extract MFA cookie from response data
        data = resp.get("data", {})
        attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
        mfa_cookie = attrs.get("twoFactorAuthenticationId", "")
        self.mfa_cookie = mfa_cookie
        return mfa_cookie

    async def trust_device(self) -> None:
        """Register the current device as trusted so future logins skip OTP."""
        await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/trustTwoFactorDevice",
            {"twoFactorAuthenticationId": self.mfa_cookie},
        )

    async def get_websocket_token(self) -> tuple[str, str]:
        """Obtain a fresh WebSocket endpoint URL and short-lived JWT token.

        The ``websockets/token`` endpoint returns a standard JSON response
        (not JSON:API): ``{"value": "<jwt>", "metaData": {"endpoint": "wss://..."}}``

        Returns:
            A ``(endpoint_url, token)`` tuple.  The token is valid for a short
            window; use it immediately to open the WebSocket connection.
        """
        resp = await self._client.get(
            "websockets/token",
            extra_headers={"Accept": "application/json"},
        )
        endpoint = resp.get("metaData", {}).get("endpoint", "")
        token = resp.get("value", "")
        return endpoint, token

    async def start_keep_alive(self) -> None:
        """Start the background keep-alive loop (idempotent).

        Sends a GET ping to ``keepAliveUrl`` every
        :data:`~pyadc.const.KEEP_ALIVE_INTERVAL_S` seconds (default 300 s) to
        prevent the Alarm.com session from expiring.
        """
        if self._keep_alive_task and not self._keep_alive_task.done():
            return
        self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())

    async def stop_keep_alive(self) -> None:
        """Cancel the keep-alive background task and await its cleanup."""
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            try:
                await self._keep_alive_task
            except asyncio.CancelledError:
                pass
            self._keep_alive_task = None

    async def _keep_alive_loop(self) -> None:
        """Send keep-alive pings every KEEP_ALIVE_INTERVAL_S seconds."""
        while True:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL_S)
            try:
                if self._keep_alive_url:
                    await self._client.get(self._keep_alive_url)
            except Exception as err:
                log.debug("Keep-alive failed: %s", err)
