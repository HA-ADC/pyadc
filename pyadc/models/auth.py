"""Authentication models for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyadc.const import OtpType


@dataclass
class TwoFactorAuthentication:
    """Two-factor authentication state."""

    available_otp_types: OtpType
    current_device_trusted: bool
    two_factor_auth_id: str = ""
