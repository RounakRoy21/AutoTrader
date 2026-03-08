"""
Tests for the health check and core API endpoints.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client):
    """Verify the /api/health endpoint returns a valid response."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "success" in data
    assert "data" in data
    assert "database" in data["data"]
    assert "redis" in data["data"]


@pytest.mark.asyncio
async def test_agent_status_endpoint(client):
    """Verify the /api/agent/status endpoint responds."""
    resp = await client.get("/api/agent/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "research_agent" in data["data"]
    assert "trading_agent" in data["data"]
