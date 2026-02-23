"""Tests for A2A HTTP server lifecycle and endpoints."""

from unittest.mock import MagicMock

import aiohttp
from aiohttp.test_utils import TestClient as AioTestClient
from aiohttp.test_utils import TestServer as AioTestServer

from amplifier_module_hooks_a2a_server.registry import A2ARegistry
from amplifier_module_hooks_a2a_server.card import build_agent_card


def _make_mock_coordinator(parent_id=None):
    """Create a minimal mock coordinator for server tests."""
    coordinator = MagicMock()
    coordinator.parent_id = parent_id
    coordinator.session_id = "test-session-123"
    coordinator.config = {
        "session": {"orchestrator": "loop-basic", "context": "context-simple"},
        "providers": [],
        "tools": [],
    }
    coordinator.register_capability = MagicMock()
    coordinator.register_cleanup = MagicMock()
    return coordinator


def _make_server(config=None):
    """Create a server with test defaults."""
    config = config or {
        "port": 0,
        "agent_name": "Test Agent",
        "agent_description": "A test agent for unit tests",
        "skills": [{"name": "testing", "description": "Good at tests"}],
    }
    registry = A2ARegistry()
    card = build_agent_card(config)
    coordinator = _make_mock_coordinator()
    from amplifier_module_hooks_a2a_server.server import A2AServer

    return A2AServer(registry, card, coordinator, config), registry


class TestAgentCardEndpoint:
    async def test_returns_agent_card_json(self):
        server, _ = _make_server()
        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get("/.well-known/agent.json")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "Test Agent"
            assert data["description"] == "A test agent for unit tests"
            assert data["version"] == "1.0"

    async def test_agent_card_has_correct_content_type(self):
        server, _ = _make_server()
        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get("/.well-known/agent.json")
            assert "application/json" in resp.headers["Content-Type"]

    async def test_agent_card_includes_skills(self):
        server, _ = _make_server()
        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get("/.well-known/agent.json")
            data = await resp.json()
            assert len(data["skills"]) == 1
            assert data["skills"][0]["name"] == "testing"


class TestGetTaskEndpoint:
    async def test_returns_completed_task(self):
        from amplifier_module_hooks_a2a_server.server import A2AServer

        config = {"port": 0, "agent_name": "Test"}
        registry = A2ARegistry()
        card = build_agent_card(config)
        coordinator = _make_mock_coordinator()
        server = A2AServer(registry, card, coordinator, config)

        # Pre-populate a task
        task_id = registry.create_task({"role": "user", "parts": [{"text": "Hi"}]})
        registry.update_task(
            task_id,
            "COMPLETED",
            artifacts=[{"parts": [{"text": "Hello back"}]}],
        )

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get(f"/a2a/v1/tasks/{task_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["id"] == task_id
            assert data["status"] == "COMPLETED"
            assert data["artifacts"][0]["parts"][0]["text"] == "Hello back"

    async def test_returns_working_task(self):
        from amplifier_module_hooks_a2a_server.server import A2AServer

        config = {"port": 0, "agent_name": "Test"}
        registry = A2ARegistry()
        card = build_agent_card(config)
        coordinator = _make_mock_coordinator()
        server = A2AServer(registry, card, coordinator, config)

        task_id = registry.create_task(
            {"role": "user", "parts": [{"text": "Working on it"}]}
        )
        registry.update_task(task_id, "WORKING")

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get(f"/a2a/v1/tasks/{task_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "WORKING"

    async def test_returns_404_for_unknown_task(self):
        from amplifier_module_hooks_a2a_server.server import A2AServer

        config = {"port": 0, "agent_name": "Test"}
        registry = A2ARegistry()
        card = build_agent_card(config)
        coordinator = _make_mock_coordinator()
        server = A2AServer(registry, card, coordinator, config)

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get("/a2a/v1/tasks/nonexistent-id")
            assert resp.status == 404

    async def test_returns_failed_task_with_error(self):
        from amplifier_module_hooks_a2a_server.server import A2AServer

        config = {"port": 0, "agent_name": "Test"}
        registry = A2ARegistry()
        card = build_agent_card(config)
        coordinator = _make_mock_coordinator()
        server = A2AServer(registry, card, coordinator, config)

        task_id = registry.create_task(
            {"role": "user", "parts": [{"text": "This will fail"}]}
        )
        registry.update_task(task_id, "FAILED", error="Something broke")

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.get(f"/a2a/v1/tasks/{task_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "FAILED"
            assert data["error"] == "Something broke"


class TestServerStartStop:
    async def test_server_starts_and_stops(self):
        server, _ = _make_server({"port": 0, "host": "127.0.0.1", "agent_name": "Test"})
        await server.start()
        assert server.port is not None
        assert server.port > 0

        # Verify server is reachable
        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{server.port}/.well-known/agent.json"
            async with session.get(url) as resp:
                assert resp.status == 200

        await server.stop()


class TestMount:
    async def test_mount_registers_capability_and_cleanup(self):
        from amplifier_module_hooks_a2a_server import mount

        coordinator = _make_mock_coordinator(parent_id=None)
        config = {
            "port": 0,
            "host": "127.0.0.1",
            "agent_name": "Mount Test",
            "known_agents": [
                {"name": "Alice", "url": "http://alice:8222"},
            ],
        }
        await mount(coordinator, config)

        # Verify capability was registered
        coordinator.register_capability.assert_called_once()
        call_args = coordinator.register_capability.call_args
        assert call_args[0][0] == "a2a.registry"
        registry = call_args[0][1]
        assert len(registry.get_agents()) == 1

        # Verify data layers were attached to the registry
        assert registry.contact_store is not None
        assert registry.pending_queue is not None

        # Verify cleanup was registered
        coordinator.register_cleanup.assert_called_once()

        # Clean up: call the cleanup function
        cleanup_fn = coordinator.register_cleanup.call_args[0][0]
        await cleanup_fn()

    async def test_mount_skips_server_in_child_session(self):
        from amplifier_module_hooks_a2a_server import mount

        coordinator = _make_mock_coordinator(parent_id="parent-123")
        await mount(coordinator, {"port": 0})

        # Should NOT register anything
        coordinator.register_capability.assert_not_called()
        coordinator.register_cleanup.assert_not_called()
