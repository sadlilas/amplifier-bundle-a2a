"""Tests for PendingQueue persistent message and approval management."""

import logging

import pytest


class TestPendingQueueEmptyMessages:
    """Tests for empty message queue behavior."""

    def test_empty_queue_has_no_pending_messages(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert queue.get_pending_messages() == []

    def test_get_message_returns_none_for_unknown_task_id(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert queue.get_message("nonexistent-id") is None


class TestPendingQueueEmptyApprovals:
    """Tests for empty approval queue behavior."""

    def test_empty_queue_has_no_pending_approvals(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert queue.get_pending_approvals() == []

    def test_get_approval_returns_none_for_unknown_task_id(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert queue.get_approval("nonexistent-id") is None


class TestPendingQueueAddMessage:
    """Tests for adding messages."""

    @pytest.mark.asyncio
    async def test_add_message_and_retrieve(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "What restaurant?"}]}
        await queue.add_message("abc-123", "http://ben:8222", "Ben's Agent", msg)

        entry = queue.get_message("abc-123")
        assert entry is not None
        assert entry["task_id"] == "abc-123"
        assert entry["sender_url"] == "http://ben:8222"
        assert entry["sender_name"] == "Ben's Agent"
        assert entry["message"] == msg
        assert entry["status"] == "pending"

    @pytest.mark.asyncio
    async def test_add_message_sets_received_at(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("abc-123", "http://ben:8222", "Ben's Agent", msg)

        entry = queue.get_message("abc-123")
        assert entry is not None
        assert "received_at" in entry
        # Should be an ISO format string
        assert "T" in entry["received_at"]

    @pytest.mark.asyncio
    async def test_get_pending_messages_returns_only_pending(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("t1", "http://a:8222", "A", msg)
        await queue.add_message("t2", "http://b:8222", "B", msg)
        await queue.update_message_status("t1", "responded")

        pending = queue.get_pending_messages()
        assert len(pending) == 1
        assert pending[0]["task_id"] == "t2"


class TestPendingQueueAddApproval:
    """Tests for adding approvals."""

    @pytest.mark.asyncio
    async def test_add_approval_and_retrieve(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello there"}]}
        await queue.add_approval("def-456", "http://unknown:8222", "Unknown Agent", msg)

        entry = queue.get_approval("def-456")
        assert entry is not None
        assert entry["task_id"] == "def-456"
        assert entry["sender_url"] == "http://unknown:8222"
        assert entry["sender_name"] == "Unknown Agent"
        assert entry["message"] == msg
        assert entry["status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_pending_approvals_returns_only_pending(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_approval("t1", "http://a:8222", "A", msg)
        await queue.add_approval("t2", "http://b:8222", "B", msg)
        await queue.update_approval_status("t1", "approved")

        pending = queue.get_pending_approvals()
        assert len(pending) == 1
        assert pending[0]["task_id"] == "t2"


class TestPendingQueueUpdateStatus:
    """Tests for status updates."""

    @pytest.mark.asyncio
    async def test_update_message_status(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("abc-123", "http://ben:8222", "Ben", msg)
        await queue.update_message_status("abc-123", "responded")

        entry = queue.get_message("abc-123")
        assert entry is not None
        assert entry["status"] == "responded"

    @pytest.mark.asyncio
    async def test_update_approval_status(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_approval("def-456", "http://unknown:8222", "Unknown", msg)
        await queue.update_approval_status("def-456", "blocked")

        entry = queue.get_approval("def-456")
        assert entry is not None
        assert entry["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_after_update_get_pending_messages_excludes_entry(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("abc-123", "http://ben:8222", "Ben", msg)
        await queue.update_message_status("abc-123", "dismissed")

        assert queue.get_pending_messages() == []


class TestPendingQueuePersistence:
    """Tests for file persistence."""

    @pytest.mark.asyncio
    async def test_data_survives_reload(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue1 = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue1.add_message("t1", "http://a:8222", "A", msg)
        await queue1.add_approval("t2", "http://b:8222", "B", msg)

        # Create a new queue from same directory — data should survive
        queue2 = PendingQueue(base_dir=tmp_path)
        msg_entry = queue2.get_message("t1")
        assert msg_entry is not None
        assert msg_entry["sender_name"] == "A"
        approval_entry = queue2.get_approval("t2")
        assert approval_entry is not None
        assert approval_entry["sender_name"] == "B"

    def test_corrupt_messages_json_starts_empty(self, tmp_path, caplog):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        path = tmp_path / "pending_messages.json"
        path.write_text("{invalid json!!")

        with caplog.at_level(logging.WARNING):
            queue = PendingQueue(base_dir=tmp_path)

        assert queue.get_pending_messages() == []
        assert any(
            "corrupt" in r.message.lower() or "unreadable" in r.message.lower()
            for r in caplog.records
        )

    def test_corrupt_approvals_json_starts_empty(self, tmp_path, caplog):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        path = tmp_path / "pending_approvals.json"
        path.write_text("{invalid json!!")

        with caplog.at_level(logging.WARNING):
            queue = PendingQueue(base_dir=tmp_path)

        assert queue.get_pending_approvals() == []
        assert any(
            "corrupt" in r.message.lower() or "unreadable" in r.message.lower()
            for r in caplog.records
        )


class TestPendingQueueEscalatedFlag:
    """Tests for the escalated field on message entries."""

    @pytest.mark.asyncio
    async def test_add_message_with_escalated_true(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Help needed"}]}
        await queue.add_message("t1", "http://a:8222", "A", msg, escalated=True)

        entry = queue.get_message("t1")
        assert entry is not None
        assert entry["escalated"] is True

    @pytest.mark.asyncio
    async def test_add_message_escalated_defaults_false(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("t1", "http://a:8222", "A", msg)

        entry = queue.get_message("t1")
        assert entry is not None
        assert entry["escalated"] is False

    @pytest.mark.asyncio
    async def test_escalated_survives_reload(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue1 = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Help"}]}
        await queue1.add_message("t1", "http://a:8222", "A", msg, escalated=True)

        # Create a new queue from same directory — escalated flag should persist
        queue2 = PendingQueue(base_dir=tmp_path)
        entry = queue2.get_message("t1")
        assert entry is not None
        assert entry["escalated"] is True


class TestPendingQueueDeferredStatus:
    """Tests for the deferred status value on messages."""

    @pytest.mark.asyncio
    async def test_update_message_status_to_deferred(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("t1", "http://a:8222", "A", msg)
        await queue.update_message_status("t1", "deferred")

        entry = queue.get_message("t1")
        assert entry is not None
        assert entry["status"] == "deferred"

    @pytest.mark.asyncio
    async def test_get_pending_messages_excludes_deferred(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("t1", "http://a:8222", "A", msg)
        await queue.add_message("t2", "http://b:8222", "B", msg)
        await queue.update_message_status("t1", "deferred")

        pending = queue.get_pending_messages()
        assert len(pending) == 1
        assert pending[0]["task_id"] == "t2"

    @pytest.mark.asyncio
    async def test_deferred_message_retrievable_by_id(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        msg = {"role": "user", "parts": [{"text": "Hello"}]}
        await queue.add_message("t1", "http://a:8222", "A", msg)
        await queue.update_message_status("t1", "deferred")

        # get_message should still find it even though it's deferred
        entry = queue.get_message("t1")
        assert entry is not None
        assert entry["status"] == "deferred"
        assert entry["task_id"] == "t1"
