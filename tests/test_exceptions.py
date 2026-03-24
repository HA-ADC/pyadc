"""Tests for pyadc exception classes."""
from pyadc.exceptions import (
    PyadcException, AuthenticationFailed, MustConfigureMfa, OtpRequired,
    SessionExpired, ServiceUnavailable, UnexpectedResponse, NotAuthorized,
    UnknownDevice, UnsupportedOperation, NotInitialized,
)


def test_pyadc_exception_base():
    exc = PyadcException("test")
    assert str(exc) == "test"
    assert isinstance(exc, Exception)


def test_otp_required_stores_types():
    exc = OtpRequired(otp_types=6)  # SMS=2 + EMAIL=4
    assert exc.otp_types == 6


def test_otp_required_message_includes_hex():
    exc = OtpRequired(otp_types=6)
    assert "0x6" in str(exc)


def test_unknown_device_stores_id():
    exc = UnknownDevice(device_id="dev-123")
    assert exc.device_id == "dev-123"


def test_unknown_device_message_includes_id():
    exc = UnknownDevice(device_id="dev-123")
    assert "dev-123" in str(exc)


def test_unexpected_response_stores_text():
    exc = UnexpectedResponse(message="oops", response_text="raw body")
    assert exc.response_text == "raw body"
    assert str(exc) == "oops"


def test_unexpected_response_defaults():
    exc = UnexpectedResponse()
    assert exc.response_text is None
    assert "Unexpected response" in str(exc)


def test_must_configure_mfa_has_message():
    exc = MustConfigureMfa()
    assert "two-factor" in str(exc).lower()


def test_inheritance_chain():
    assert issubclass(MustConfigureMfa, AuthenticationFailed)
    assert issubclass(OtpRequired, AuthenticationFailed)
    assert issubclass(AuthenticationFailed, PyadcException)
    assert issubclass(SessionExpired, PyadcException)
    assert issubclass(ServiceUnavailable, PyadcException)
    assert issubclass(NotAuthorized, PyadcException)
    assert issubclass(UnknownDevice, PyadcException)
    assert issubclass(UnsupportedOperation, PyadcException)
    assert issubclass(NotInitialized, PyadcException)
    assert issubclass(UnexpectedResponse, PyadcException)


def test_all_base_exceptions_are_catchable_as_pyadc_exception():
    for cls in (
        AuthenticationFailed, SessionExpired,
        ServiceUnavailable, NotAuthorized, UnsupportedOperation, NotInitialized,
    ):
        try:
            raise cls("msg")
        except PyadcException:
            pass
        else:
            raise AssertionError(f"{cls} not caught as PyadcException")

    # MustConfigureMfa uses a fixed no-arg message by design
    try:
        raise MustConfigureMfa()
    except PyadcException:
        pass
    else:
        raise AssertionError("MustConfigureMfa not caught as PyadcException")
