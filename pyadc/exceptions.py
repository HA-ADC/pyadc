"""Exceptions for the pyadc library."""

from __future__ import annotations


class PyadcException(Exception):
    """Base exception for pyadc."""


# --- Authentication ---


class AuthenticationFailed(PyadcException):
    """Server rejected the provided credentials."""


class MustConfigureMfa(AuthenticationFailed):
    """Account requires MFA to be configured before login can proceed."""

    def __init__(self) -> None:
        super().__init__(
            "Alarm.com requires that two-factor authentication be set up on your account. "
            "Please log in to Alarm.com and set up two-factor authentication."
        )


class OtpRequired(AuthenticationFailed):
    """Two-factor authentication code is required to complete login.

    Args:
        otp_types: Bitmask of enabled OTP methods (see OtpType in const.py).
                   1 = app, 2 = SMS, 4 = email.
    """

    def __init__(self, otp_types: int) -> None:
        super().__init__(f"OTP required (enabled types bitmask: {otp_types:#x}).")
        self.otp_types = otp_types


# --- Session / Connectivity ---


class SessionExpired(PyadcException):
    """The current session has timed out and must be refreshed."""


class ServiceUnavailable(PyadcException):
    """The Alarm.com server returned a 5xx error."""


# --- Response / Protocol ---


class UnexpectedResponse(PyadcException):
    """Raised when the API returns an unexpected HTTP status or body."""

    def __init__(self, message: str = "Unexpected response", response_text: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.response_text = response_text
        self.status_code = status_code


# --- Authorization ---


class NotAuthorized(PyadcException):
    """The authenticated user does not have permission to perform this action (HTTP 403)."""


# --- Device ---


class UnknownDevice(PyadcException):
    """The requested device ID was not found in the device registry.

    Args:
        device_id: The device identifier that could not be resolved.
    """

    def __init__(self, device_id: str) -> None:
        super().__init__(f"Unknown device ID '{device_id}'.")
        self.device_id = device_id


class UnsupportedOperation(PyadcException):
    """The requested operation is not supported for this device type."""


# --- Lifecycle ---


class NotInitialized(PyadcException):
    """AlarmBridge was used before `.initialize()` was called."""
