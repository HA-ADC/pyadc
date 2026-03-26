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
from yarl import URL

from pyadc.const import (
    KEEP_ALIVE_FAILURE_WARN_LIMIT,
    KEEP_ALIVE_INTERVAL_S,
    KEEP_ALIVE_MAX_INTERVAL_S,
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
        seamless_token: str = "",
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
            seamless_token: Pre-stored seamless login token (the ``ST`` browser
                cookie value).  When supplied, :meth:`login` will attempt a
                lightweight token-based login before falling back to full
                credential submission.  Obtain this value by reading
                :attr:`seamless_token` after a successful :meth:`login`.
        """
        self._client = client
        self._session = session
        self._username = username
        self._password = password
        self._mfa_cookie: str = mfa_cookie
        if mfa_cookie:
            self._client._mfa_cookie = mfa_cookie
        self._seamless_token: str = seamless_token
        self._login_lock: asyncio.Lock = asyncio.Lock()
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
        self._user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

    @property
    def mfa_cookie(self) -> str:
        """Return the stored MFA cookie."""
        return self._mfa_cookie

    @mfa_cookie.setter
    def mfa_cookie(self, value: str) -> None:
        """Set MFA cookie and keep the HTTP client in sync."""
        self._mfa_cookie = value
        self._client._mfa_cookie = value

    @property
    def seamless_token(self) -> str:
        """Return the current seamless login token (``ST`` cookie value).

        This value rotates on every successful seamless login.  Persist it
        to storage after each :meth:`login` call and pass it back in on the
        next startup to avoid a full credential round-trip.
        """
        return self._seamless_token

    async def login(self) -> None:
        """Execute the Alarm.com login flow.

        If a :attr:`seamless_token` is available, attempts a lightweight
        token-based login first (1–2 HTTP requests instead of 4–6).  Falls
        back to full credential submission if the token is missing, invalid,
        or expired.  Either path calls :meth:`_load_user_data` to populate
        the keep-alive URL and user ID.

        A re-entrancy lock ensures that if multiple coroutines trigger
        ``login()`` simultaneously (e.g., two device controllers both hit a
        403 at session expiry), only one full login proceeds; the others wait
        and reuse the resulting session.

        Steps (full credential path):
        1. Scrape hidden ASP.NET form fields from the login page.
        2. POST credentials; detect failure from redirect URL parameters.
        3. Load identity/profile data to get user ID and keep-alive URL.
        4. Query 2FA status; raise :exc:`~pyadc.exceptions.OtpRequired` if
           the device is not trusted and OTP is required.

        Raises:
            AuthenticationFailed: On bad credentials or locked account.
            OtpRequired: When two-factor authentication is required.
            ServiceUnavailable: On 5xx responses from Alarm.com.
        """
        async with self._login_lock:
            # Discard the stale AFG anti-forgery token from the previous session.
            # It must not be sent on any request issued during re-auth; each login
            # response will supply a fresh value via _update_afg_from_response().
            self._client._afg_token = ""

            if self._seamless_token:
                if await self._try_seamless_login():
                    return
                log.info("Seamless login failed — falling back to full credential login")

            # Clear stale session cookies before the full credential path.
            # Seamless login uses explicit raw Cookie headers (jar-independent), so
            # this only runs after seamless has already been tried and failed.
            # An expired session cookie in the jar could confuse the login-page
            # scrape or cause the server to mis-classify the request.
            self._session.cookie_jar.clear()

            await self._scrape_login_page()
            await self._submit_credentials()
            await self._load_user_data()
            await self._check_two_factor()
            self._extract_seamless_token()

    async def _try_seamless_login(self) -> bool:
        """Attempt a token-based login using the stored ``ST`` (seamless) cookie.

        Builds a raw ``Cookie`` header containing the ST token and (if present)
        the ``twoFactorAuthenticationId`` cookie, then scrapes the login form for
        ViewState fields and POSTs to ``Default.aspx`` *without* credentials.
        When the server sees the ``ST`` cookie alongside ``IsFromNewSite=1``, it
        calls ``ProcessLoginWithSeamlessCookieToken`` instead of credential
        processing, bypassing 2FA if the token was created with
        ``bypass_two_factor_auth=true``.

        The raw Cookie header is used (instead of aiohttp's cookie jar / SimpleCookie)
        because standard base64 tokens contain ``+``, ``/``, and ``=`` which Python's
        ``SimpleCookie`` wraps in double-quotes.  ASP.NET then receives the value WITH
        the quotes and ``Convert.FromBase64String`` throws ``FormatException``.

        Returns:
            ``True`` on success; ``False`` if the token is invalid/expired
            (clears :attr:`_seamless_token` so the caller falls back to
            full credential login).
        """
        try:
            log.debug("Seamless login: using stored token")

            # Build a raw Cookie header string for the POST (ST + MFA).  Standard base64
            # tokens contain +, /, and = which Python's SimpleCookie wraps in double-quotes
            # (e.g. ST="abc+def/ghi==").  ASP.NET then reads the value WITH the quotes and
            # Convert.FromBase64String fails.  A raw header string avoids this quoting entirely.
            #
            # IMPORTANT: the ST cookie must NOT be sent on the GET to login.aspx.
            # NewPublic/LoginForm.ascx.cs reads the ST cookie during page render and, for
            # non-touch desktop browsers, DELETES the token from the DB and clears the cookie.
            # Only the POST to Default.aspx should carry the ST cookie.
            post_cookie_parts = [f"ST={self._seamless_token}"]
            if self._mfa_cookie:
                post_cookie_parts.append(f"twoFactorAuthenticationId={self._mfa_cookie}")
                log.debug("Seamless login: MFA cookie set")
            post_raw_cookie = "; ".join(post_cookie_parts)

            # GET cookie: MFA only — no ST so the login form doesn't delete the token
            get_raw_cookie = f"twoFactorAuthenticationId={self._mfa_cookie}" if self._mfa_cookie else None

            # Scrape form fields (GET to login page — server renders form, doesn't auto-login on GET)
            get_headers: dict[str, str] = {"User-Agent": self._user_agent}
            if get_raw_cookie:
                get_headers["Cookie"] = get_raw_cookie
            async with self._session.get(
                self._login_url,
                allow_redirects=True,
                headers=get_headers,
            ) as resp:
                html = await resp.text()

            soup = _FormParser()
            soup.feed(html)
            form_fields = soup.get_target_form_fields()
            if form_fields is None:
                log.info("Seamless login: login form not found in GET response (final_url=%s) — server may have auto-redirected", self._login_url)
                self._seamless_token = ""
                return False

            # Build POST body from all form fields (ViewState etc.) but without credentials
            data = dict(form_fields)
            if "JavaScriptTest" in data:
                data["JavaScriptTest"] = "1"
            if "IsFromNewSite" in data:
                data["IsFromNewSite"] = "1"
            # Remove submit button — not needed and can confuse ASP.NET page lifecycle
            for k in list(data.keys()):
                if k.endswith("butLogin") or k.endswith("signInButton"):
                    data.pop(k)

            log.debug("Attempting seamless login POST to %s", self._form_submit_url)
            async with self._session.post(
                self._form_submit_url,
                data=data,
                headers={
                    "User-Agent": self._user_agent,
                    "Referrer": self._login_url,
                    "Cookie": post_raw_cookie,
                },
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
                if resp.status in (301, 302, 303, 307, 308) and location:
                    final_url = location if location.startswith("http") else f"{self._base_url}{location}"
                    async with self._session.get(
                        final_url,
                        allow_redirects=True,
                    ) as final_resp:
                        final_url = str(final_resp.url)
                else:
                    final_url = str(resp.url)

                # Detect failure — server redirects to login page or appends error param
                path_lower = final_url.split("?")[0].lower()
                if (
                    "m=login_fail" in final_url
                    or "m=lockedout" in final_url.lower()
                    or ("/login" in path_lower and "/web/" not in path_lower)
                ):
                    log.info(
                        "Seamless login token rejected by server — clearing token (final_url=%s)",
                        final_url,
                    )
                    self._seamless_token = ""
                    return False

                # Capture AFG token from this response
                afg = resp.cookies.get("afg") or self._session.cookie_jar.filter_cookies(resp.url).get("afg")
                if afg:
                    self._client._afg_token = afg.value

        except (
            aiohttp.ClientPayloadError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerTimeoutError,
            asyncio.TimeoutError,
        ) as err:
            # Transient network error — token is still valid, propagate so the
            # caller can retry rather than falling back to full credential login.
            log.warning("Seamless login transient network error (%s) — keeping token", err)
            raise
        except Exception as err:
            log.info("Seamless login raised exception (%s) — clearing token", err)
            self._seamless_token = ""
            return False

        # Extract the newly rotated ST cookie from the session jar
        self._extract_seamless_token()

        # Still need user data for keep-alive URL
        await self._load_user_data()
        log.info("Seamless login succeeded")
        return True

    def _extract_seamless_token(self) -> None:
        """Read the ``ST`` (seamless login) cookie from the session jar.

        Called after every successful login — full credential or seamless.
        The value is only present if the server issued it (i.e. the
        ``chkKeepMeLoggedIn`` checkbox was set in the credential POST, or the
        previous seamless login triggered token rotation).
        """
        try:
            jar = self._session.cookie_jar.filter_cookies(URL(self._base_url))
            st = jar.get("ST")
            if st and st.value:
                if st.value != self._seamless_token:
                    log.debug(
                        "Seamless login token captured/rotated from session jar "
                        "(len=%d, prefix=%.8s…)",
                        len(st.value),
                        st.value,
                    )
                self._seamless_token = st.value
            else:
                # Dump all jar cookies to diagnose why ST is missing
                all_cookies = [(c.key, len(c.value)) for c in self._session.cookie_jar]
                log.debug(
                    "No ST cookie in session jar after login. Jar keys+lengths: %s",
                    all_cookies,
                )
        except Exception:
            pass

    async def _scrape_login_page(self) -> None:
        """GET login page and dynamically extract ALL form field names and values.

        Follows redirects so the final URL (which is the POST target) is captured.
        Field names are ASP.NET-generated and must be read from the rendered HTML —
        they cannot be hardcoded because they depend on the NamingContainer hierarchy.
        """
        mfa_cookies = {"twoFactorAuthenticationId": self._mfa_cookie} if self._mfa_cookie else None
        async with self._session.get(
            self._login_url,
            allow_redirects=True,
            cookies=mfa_cookies,
            headers={"User-Agent": self._user_agent},
        ) as resp:
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
        # Request a seamless login token by checking "Keep Me Logged In".
        # The server creates an ST cookie when this field is "on", which we persist
        # and reuse on subsequent startups to avoid full credential round-trips.
        # MainHandler.cs checks request.Form for any key containing "chkKeepMeLoggedIn"
        # (not "chkRememberMe" — that's a different checkbox that doesn't trigger the ST
        # cookie). The CustomerDotNet form only renders chkRememberMe, so we inject
        # chkKeepMeLoggedIn directly into the POST body instead of relying on the scrape.
        keep_logged_in_field = next(
            (k for k in data if "chkKeepMeLoggedIn" in k), None
        )
        if keep_logged_in_field:
            data[keep_logged_in_field] = "on"
        else:
            data["chkKeepMeLoggedIn"] = "on"
        log.debug("Keep-me-logged-in field injected into POST body")

        log.debug("Submitting login POST to %s", self._form_submit_url)
        # Inject twoFactorAuthenticationId so the server recognises this as a trusted device
        mfa_cookies = {"twoFactorAuthenticationId": self._mfa_cookie} if self._mfa_cookie else None
        async with self._session.post(
            self._form_submit_url,
            data=data,
            headers={"User-Agent": self._user_agent, "Referrer": self._login_url},
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
            # Capture the 2FA device ID the server assigned during login so it
            # can be replayed on future logins to skip the OTP challenge.
            tfa_cookie = self._session.cookie_jar.filter_cookies(resp.url).get("twoFactorAuthenticationId")
            if tfa_cookie and tfa_cookie.value:
                self._mfa_cookie = tfa_cookie.value
                log.debug("2FA device ID captured from login response")

    async def _load_user_data(self) -> None:
        """Load identity data to obtain the user ID and keep-alive URL.

        The ``identities`` endpoint returns both the login ID (used for 2FA
        queries) and ``keepAliveUrl`` (an absolute URL to ``KeepAlive.aspx``
        that must be pinged every :data:`~pyadc.const.KEEP_ALIVE_INTERVAL_S`
        seconds to prevent HTTP session expiry).
        """
        try:
            identity_resp = await self._client.get("identities")
        except Exception as err:
            log.warning("Failed to load identity data: %s", err)
            return

        data = identity_resp.get("data")
        if not data:
            log.warning("identities response contained no data")
            return

        items = data if isinstance(data, list) else [data]
        if not items:
            return

        first = items[0]
        self._user_id = first.get("id", "")
        attrs = first.get("attributes", {})

        # keepAliveUrl is nested inside applicationSessionProperties.
        # The server may return a relative path (e.g. "/web/KeepAlive.aspx");
        # resolve it to an absolute URL using the configured base URL.
        session_props = attrs.get("applicationSessionProperties", {})
        raw_url = session_props.get("keepAliveUrl", session_props.get("keep_alive_url", ""))
        if raw_url and not raw_url.startswith("http"):
            self._keep_alive_url = f"{self._base_url}{raw_url}"
        else:
            self._keep_alive_url = raw_url

        if not self._keep_alive_url:
            log.warning(
                "keepAliveUrl not found in identities applicationSessionProperties — "
                "HTTP session will expire in ~20 minutes. applicationSessionProperties keys: %s",
                list(session_props.keys()),
            )

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
            if "423" in str(exc):
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
        """Send keep-alive pings every KEEP_ALIVE_INTERVAL_S seconds.

        If the keep-alive URL was not acquired at login, attempts to re-fetch
        it from the profile endpoint before each ping.  Consecutive failures
        apply exponential back-off (capped at
        :data:`~pyadc.const.KEEP_ALIVE_MAX_INTERVAL_S`) to reduce backend
        load when the server is unreachable.
        """
        consecutive_failures = 0
        while True:
            # Back off on consecutive failures to avoid hammering a degraded backend
            if consecutive_failures > 0:
                backoff = min(
                    KEEP_ALIVE_INTERVAL_S * (2 ** (consecutive_failures - 1)),
                    KEEP_ALIVE_MAX_INTERVAL_S,
                )
                await asyncio.sleep(backoff)
            else:
                await asyncio.sleep(KEEP_ALIVE_INTERVAL_S)

            if not self._keep_alive_url:
                log.warning("Keep-alive URL not set — attempting identity re-fetch")
                try:
                    await self._load_user_data()
                except Exception as err:
                    log.warning("Keep-alive URL re-fetch failed: %s", err)
                if not self._keep_alive_url:
                    log.warning("Keep-alive URL still unavailable — session will expire in ~20 minutes")
                    continue

            try:
                # KeepAlive.aspx is an ASPX page (not a JSON API endpoint) —
                # use plain browser-like headers; the session cookie jar carries
                # the auth cookies accumulated during login.
                async with self._session.get(
                    self._keep_alive_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referrer": f"{self._base_url}/web/system/home",
                    },
                    cookies=self._client._mfa_cookies(),
                ) as resp:
                    ok = resp.status < 400
                if resp.status == 401:
                    raise AuthenticationFailed(
                        f"Keep-alive returned HTTP 401 — session expired"
                    )
                if ok:
                    if consecutive_failures > 0:
                        log.info("Keep-alive recovered after %d failure(s)", consecutive_failures)
                    consecutive_failures = 0
                    log.debug("Keep-alive ping sent (status=%s)", resp.status)
                else:
                    raise RuntimeError(f"Keep-alive returned HTTP {resp.status}")
            except AuthenticationFailed:
                raise  # session expired — propagate to kill the keep-alive task
            except Exception as err:
                consecutive_failures += 1
                if consecutive_failures >= KEEP_ALIVE_FAILURE_WARN_LIMIT:
                    log.warning(
                        "Keep-alive has failed %d consecutive time(s) — session may be expiring: %s",
                        consecutive_failures,
                        err,
                    )
                else:
                    log.debug("Keep-alive failed: %s", err)
