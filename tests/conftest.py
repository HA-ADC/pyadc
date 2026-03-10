"""Shared pytest fixtures for pyadc tests."""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=aiohttp.ClientSession)
    return session


@pytest.fixture
def mock_response():
    """Factory for mock aiohttp responses."""
    def _make(status=200, json_data=None, cookies=None):
        resp = AsyncMock()
        resp.status = status
        resp.url = "https://www.alarm.com/web/api/test"
        resp.json = AsyncMock(return_value=json_data or {})
        resp.cookies = cookies or {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp
    return _make
