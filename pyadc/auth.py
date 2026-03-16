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
from html.parser import HTMLParser

from pyadc.const import (
    KEEP_ALIVE_INTERVAL_S,
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


class _FormParser(HTMLParser):
    """Minimal stdlib HTML parser that extracts <input> fields from a login form.

    Prefers the form with id="form1" (ASP.NET default); falls back to the
    first form found on the page.
    """

    def __init__(self) -> None:
        super().__init__()
        self._forms: list[dict] = []
        self._current_form: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "form":
            self._current_form = {"id": attr_dict.get("id") or "", "fields": {}}
            self._forms.append(self._current_form)
        elif tag == "input" and self._current_form is not None:
            name = attr_dict.get("name")
            if name:
                self._current_form["fields"][name] = attr_dict.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current_form = None

    def get_target_form_fields(self) -> dict[str, str] | None:
        """Return fields from form#form1, or from the first form if not found."""
        if not self._forms:
            return None
        for form in self._forms:
            if form["id"] == "form1":
                return form["fields"]
        return self._forms[0]["fields"]


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
        base_url: str = URL_BASE,
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
            base_url: Root URL of the Alarm.com deployment.  Defaults to the
                production endpoint; override to target a dev/staging server.
        """
        self._client = client
        self._session = session
        self._username = username
        self._password = password
        self._mfa_cookie: str = mfa_cookie
        if mfa_cookie:
            self._client._mfa_cookie = mfa_cookie
        self._keep_alive_url: str = ""
        self._user_id: str = ""
        self._keep_alive_task: asyncio.Task | None = None
        self._form_fields: dict[str, str] = {}
        self._base_url: str = base_url
        self._login_url: str = f"{base_url}/login.aspx"
        self._form_submit_url: str = f"{base_url}/web/Default.aspx"
        self._username_field: str | None = None
        self._password_field: str | None = None
        self._submit_field: str | None = None

    @property
    def mfa_cookie(self) -> str:
        """Return the stored MFA cookie."""
        return self._mfa_cookie

    @mfa_cookie.setter
    def mfa_cookie(self, value: str) -> None:
        """Set MFA cookie and keep the HTTP client in sync."""
        self._mfa_cookie = value
        self._client._mfa_cookie = value

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
        mfa_cookies = {"twoFactorAuthenticationId": self._mfa_cookie} if self._mfa_cookie else None
        async with self._session.get(self._login_url, allow_redirects=True, cookies=mfa_cookies) as resp:
            html = await resp.text()

        soup = _FormParser()
        soup.feed(html)
        self._form_fields = soup.get_target_form_fields()
        if self._form_fields is None:
            raise AuthenticationFailed("Could not find login form on page")

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
        # CustomerDotNet is deployed under /web/, so the actual POST target is _form_submit_url.
        self._form_submit_url = f"{self._base_url}/web/Default.aspx"

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
        # Inject twoFactorAuthenticationId so the server recognises this as a trusted device
        mfa_cookies = {"twoFactorAuthenticationId": self._mfa_cookie} if self._mfa_cookie else None
        async with self._session.post(
            self._form_submit_url,
            data=data,
            headers={"User-Agent": "Mozilla/5.0", "Referrer": self._login_url},
            cookies=mfa_cookies,
            allow_redirects=False,
        ) as resp:
            location = resp.headers.get("Location", "")
            # Follow the redirect to complete the login sequence and accumulate cookies
            if resp.status in (301, 302, 303, 307, 308) and location:
                async with self._session.get(
                    location if location.startswith("http") else f"{self._base_url}{location}",
                    allow_redirects=True,
                    cookies=mfa_cookies,
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
        log.debug("_check_two_factor attrs: %s", attrs)

        # Check if current device is trusted
        trusted = attrs.get("isCurrentDeviceTrusted", attrs.get("is_current_device_trusted", False))
        log.debug("_check_two_factor: isCurrentDeviceTrusted=%s otp_mask=%s", trusted, attrs.get("enabledTwoFactorTypes"))
        if trusted:
            return

        # Check available OTP types
        otp_type_mask = attrs.get("enabledTwoFactorTypes", attrs.get("enabled_two_factor_types", 0))
        if otp_type_mask:
            raise OtpRequired(otp_types=otp_type_mask)

    async def send_otp_sms(self) -> None:
        """Trigger Alarm.com to send an OTP code via SMS to the account's phone."""
        await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/sendTwoFactorAuthenticationCodeViaSms",
            extra_headers={"Accept": "application/json"},
        )

    async def send_otp_email(self) -> None:
        """Trigger Alarm.com to send an OTP code via e-mail to the account address."""
        await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/sendTwoFactorAuthenticationCodeViaEmail",
            extra_headers={"Accept": "application/json"},
        )

    async def verify_otp(self, code: str, otp_type: int = 0) -> str:
        """Submit the OTP code to complete the two-factor challenge.

        Args:
            code: The numeric code received via SMS, e-mail, or authenticator app.
            otp_type: The OtpType integer value used to request the code
                (2=SMS, 4=email, 1=app).  Must match what was sent.

        Returns:
            The ``twoFactorAuthenticationId`` cookie value.  Store this as
            ``mfa_cookie`` to skip future OTP challenges on this device.

        Raises:
            AuthenticationFailed: HTTP 423 — code was wrong or has expired.
        """
        from pyadc.exceptions import UnexpectedResponse  # avoid circular at top level
        try:
            await self._client.post(
                f"{TWO_FACTOR_PATH}/{self._user_id}/verifyTwoFactorCode",
                {"code": code, "typeOf2FA": otp_type},
                extra_headers={"Accept": "application/json"},
            )
        except UnexpectedResponse as exc:
            if exc.status_code == 423:
                raise AuthenticationFailed(
                    "Verification code is incorrect or has expired."
                ) from exc
            raise
        # ADC sets twoFactorAuthenticationId as an HTTP response cookie.
        # _update_afg_from_response (called inside client.post) already captured
        # it into self._client._mfa_cookie — sync it back to self.mfa_cookie.
        mfa_cookie = self._client._mfa_cookie
        if mfa_cookie:
            self._mfa_cookie = mfa_cookie  # update backing field without re-pushing to client
            log.debug("MFA cookie captured from verifyTwoFactorCode response: len=%d", len(mfa_cookie))
        else:
            log.warning("verifyTwoFactorCode response did not set twoFactorAuthenticationId cookie")
        return self._mfa_cookie

    async def trust_device(self, device_name: str = "pyadc HA integration") -> None:
        """Register the current device as trusted so future logins skip OTP."""
        await self._client.post(
            f"{TWO_FACTOR_PATH}/{self._user_id}/trustTwoFactorDevice",
            {"deviceName": device_name},
            extra_headers={"Accept": "application/json"},
        )
        # The server sets twoFactorAuthenticationId after trustTwoFactorDevice
        if self._client._mfa_cookie:
            self._mfa_cookie = self._client._mfa_cookie
            log.info("trust_device: MFA cookie captured (len=%d)", len(self._mfa_cookie))
        else:
            # Last resort: scan entire cookie jar
            try:
                for cookie in self._client._session.cookie_jar:
                    if cookie.key == "twoFactorAuthenticationId" and cookie.value:
                        self._mfa_cookie = cookie.value
                        self._client._mfa_cookie = cookie.value
                        log.info("trust_device: MFA cookie from jar scan (len=%d)", len(self._mfa_cookie))
                        break
            except Exception:
                pass
            if not self._mfa_cookie:
                log.warning("trust_device: twoFactorAuthenticationId cookie not found after trust call")

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
                else:
                    log.error(
                        "Keep-alive URL was not set during login; session will expire. "
                        "Check that profile data loaded successfully."
                    )
            except Exception as err:
                log.debug("Keep-alive failed: %s", err)
