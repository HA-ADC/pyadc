"""Tests for AdcClient HTTP methods and error mapping."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from pyadc.client import AdcClient
from pyadc.exceptions import (
    NotAuthorized, AuthenticationFailed, ServiceUnavailable, UnexpectedResponse,
)


@pytest.fixture
def client(mock_session):
    return AdcClient(mock_session)


# ---------------------------------------------------------------------------
# Successful GET
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_success(client, mock_response):
    resp = mock_response(200, {"data": [{"id": "1"}]})
    client._session.get = MagicMock(return_value=resp)
    result = await client.get("devices/partition")
    assert result == {"data": [{"id": "1"}]}


@pytest.mark.asyncio
async def test_get_builds_full_url(client, mock_response):
    resp = mock_response(200, {})
    client._session.get = MagicMock(return_value=resp)
    await client.get("devices/partition")
    call_args = client._session.get.call_args
    url = call_args[0][0]
    assert url.startswith("https://www.alarm.com/web/api/")
    assert "devices/partition" in url


@pytest.mark.asyncio
async def test_get_absolute_url_not_prefixed(client, mock_response):
    resp = mock_response(200, {})
    client._session.get = MagicMock(return_value=resp)
    await client.get("https://custom.example.com/api/test")
    call_args = client._session.get.call_args
    url = call_args[0][0]
    assert url == "https://custom.example.com/api/test"


# ---------------------------------------------------------------------------
# HTTP error → exception mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_403_raises_not_authorized(client, mock_response):
    resp = mock_response(403)
    client._session.get = MagicMock(return_value=resp)
    with pytest.raises(NotAuthorized):
        await client.get("devices/partition")


@pytest.mark.asyncio
async def test_get_401_raises_auth_failed(client, mock_response):
    resp = mock_response(401)
    client._session.get = MagicMock(return_value=resp)
    with pytest.raises(AuthenticationFailed):
        await client.get("devices/partition")


@pytest.mark.asyncio
async def test_get_500_raises_service_unavailable(client, mock_response):
    resp = mock_response(500)
    client._session.get = MagicMock(return_value=resp)
    with pytest.raises(ServiceUnavailable):
        await client.get("devices/partition")


@pytest.mark.asyncio
async def test_get_503_raises_service_unavailable(client, mock_response):
    resp = mock_response(503)
    client._session.get = MagicMock(return_value=resp)
    with pytest.raises(ServiceUnavailable):
        await client.get("devices/partition")


@pytest.mark.asyncio
async def test_get_404_raises_unexpected_response(client, mock_response):
    resp = mock_response(404)
    client._session.get = MagicMock(return_value=resp)
    with pytest.raises(UnexpectedResponse):
        await client.get("devices/partition")


@pytest.mark.asyncio
async def test_get_422_raises_unexpected_response(client, mock_response):
    resp = mock_response(422)
    client._session.get = MagicMock(return_value=resp)
    with pytest.raises(UnexpectedResponse):
        await client.get("devices/partition")


# ---------------------------------------------------------------------------
# AFG anti-forgery header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_afg_header_sent_when_present(client, mock_response):
    client._afg_token = "test-afg-token"
    resp = mock_response(200, {})
    client._session.get = MagicMock(return_value=resp)
    await client.get("test")
    call_kwargs = client._session.get.call_args
    headers = call_kwargs[1].get("headers", {})
    assert "Ajaxrequestuniquekey" in headers
    assert headers["Ajaxrequestuniquekey"] == "test-afg-token"


@pytest.mark.asyncio
async def test_afg_header_not_sent_when_empty(client, mock_response):
    client._afg_token = ""
    resp = mock_response(200, {})
    client._session.get = MagicMock(return_value=resp)
    await client.get("test")
    call_kwargs = client._session.get.call_args
    headers = call_kwargs[1].get("headers", {})
    assert "Ajaxrequestuniquekey" not in headers


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_success(client, mock_response):
    resp = mock_response(200, {"result": "ok"})
    client._session.post = MagicMock(return_value=resp)
    result = await client.post("devices/lock/1/lock", body={"state": 1})
    assert result == {"result": "ok"}


@pytest.mark.asyncio
async def test_post_403_raises_not_authorized(client, mock_response):
    resp = mock_response(403)
    client._session.post = MagicMock(return_value=resp)
    with pytest.raises(NotAuthorized):
        await client.post("devices/lock/1/lock")
