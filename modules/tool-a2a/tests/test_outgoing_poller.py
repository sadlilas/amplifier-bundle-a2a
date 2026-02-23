"""Tests for background outgoing task poller."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_tool_a2a import A2ATool


def _make_tool(poll_interval: float = 0.1) -> A2ATool:
    """Create an A2ATool with mocked coordinator and client."""
    coordinator = MagicMock()
    coordinator.get_capability = MagicMock(return_value=None)
    coordinator.mount = AsyncMock()
    coordinator.register_cleanup = MagicMock()
    config = {"poll_interval": poll_interval}
    tool = A2ATool(coordinator, config)
    tool.client = MagicMock()
    tool.client.get_task_status = AsyncMock()
    tool.client.close = AsyncMock()
    return tool


class TestPollerMovesCompleted:
    """Poller moves tasks with terminal status to _completed_outgoing."""

    @pytest.mark.asyncio
    async def test_poller_moves_completed_to_outgoing(self):
        tool = _make_tool(poll_interval=0.1)
        tool.client.get_task_status.return_value = {
            "id": "task-1",
            "status": "COMPLETED",
            "artifacts": [{"parts": [{"text": "Done"}]}],
        }

        # Manually add a pending outgoing task
        tool._pending_outgoing["task-1"] = {
            "task_id": "task-1",
            "agent_url": "http://remote:8222",
            "agent_name": "remote",
            "submitted_at": "2025-01-01T00:00:00+00:00",
        }

        tool._start_poller()
        await asyncio.sleep(0.3)
        await tool._stop_poller()

        # Task should have been moved to completed
        assert len(tool._completed_outgoing) == 1
        assert tool._completed_outgoing[0]["task_id"] == "task-1"
        assert tool._completed_outgoing[0]["result"]["status"] == "COMPLETED"


class TestPollerIgnoresNonTerminal:
    """Poller leaves non-terminal tasks in _pending_outgoing."""

    @pytest.mark.asyncio
    async def test_poller_ignores_non_terminal_tasks(self):
        tool = _make_tool(poll_interval=0.1)
        tool.client.get_task_status.return_value = {
            "id": "task-2",
            "status": "WORKING",
        }

        tool._pending_outgoing["task-2"] = {
            "task_id": "task-2",
            "agent_url": "http://remote:8222",
            "agent_name": "remote",
            "submitted_at": "2025-01-01T00:00:00+00:00",
        }

        tool._start_poller()
        await asyncio.sleep(0.3)
        await tool._stop_poller()

        # Task should still be pending, not moved to completed
        assert "task-2" in tool._pending_outgoing
        assert len(tool._completed_outgoing) == 0


class TestPollerHandlesErrors:
    """Poller handles connection errors without losing tasks."""

    @pytest.mark.asyncio
    async def test_poller_handles_connection_errors(self):
        tool = _make_tool(poll_interval=0.1)
        tool.client.get_task_status.side_effect = ConnectionError(
            "Connection refused"
        )

        tool._pending_outgoing["task-3"] = {
            "task_id": "task-3",
            "agent_url": "http://remote:8222",
            "agent_name": "remote",
            "submitted_at": "2025-01-01T00:00:00+00:00",
        }

        tool._start_poller()
        await asyncio.sleep(0.3)
        await tool._stop_poller()

        # Task should still be in pending (not lost)
        assert "task-3" in tool._pending_outgoing
        assert len(tool._completed_outgoing) == 0


class TestPollerCancellation:
    """Poller cancels cleanly without exceptions."""

    @pytest.mark.asyncio
    async def test_poller_cancels_cleanly(self):
        tool = _make_tool(poll_interval=0.1)

        tool._start_poller()
        assert tool._poller_task is not None
        assert not tool._poller_task.done()

        await tool._stop_poller()

        # Poller task should be cleaned up
        assert tool._poller_task is None


class TestPollerInterval:
    """Poller respects the configured poll interval."""

    @pytest.mark.asyncio
    async def test_poller_respects_poll_interval(self):
        tool = _make_tool(poll_interval=0.1)
        tool.client.get_task_status.return_value = {
            "id": "task-4",
            "status": "WORKING",
        }

        tool._pending_outgoing["task-4"] = {
            "task_id": "task-4",
            "agent_url": "http://remote:8222",
            "agent_name": "remote",
            "submitted_at": "2025-01-01T00:00:00+00:00",
        }

        tool._start_poller()
        await asyncio.sleep(0.3)
        await tool._stop_poller()

        # With 0.1s interval and 0.3s wait, should have polled 2-3 times
        assert tool.client.get_task_status.call_count >= 2


class TestPollerRemovesFromPending:
    """After moving to completed, task is removed from _pending_outgoing."""

    @pytest.mark.asyncio
    async def test_poller_removes_from_pending_on_completion(self):
        tool = _make_tool(poll_interval=0.1)
        tool.client.get_task_status.return_value = {
            "id": "task-5",
            "status": "FAILED",
            "error": "Something went wrong",
        }

        tool._pending_outgoing["task-5"] = {
            "task_id": "task-5",
            "agent_url": "http://remote:8222",
            "agent_name": "remote",
            "submitted_at": "2025-01-01T00:00:00+00:00",
        }

        tool._start_poller()
        await asyncio.sleep(0.3)
        await tool._stop_poller()

        # Task should no longer be in pending
        assert "task-5" not in tool._pending_outgoing
        # And should be in completed
        assert len(tool._completed_outgoing) == 1
        assert tool._completed_outgoing[0]["result"]["status"] == "FAILED"
