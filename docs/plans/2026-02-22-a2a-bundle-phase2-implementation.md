# A2A Bundle Phase 2 Implementation Plan

> **Execution:** Use the subagent-driven-development workflow to implement this plan.

**Goal:** Add discovery, trust, and human-in-the-loop capabilities to `amplifier-bundle-a2a` — mDNS auto-discovery, contact list with first-contact approval, per-contact trust tiers with capability scoping, Mode A (notify-and-wait), async send, and LLM confidence evaluation for Mode C → A escalation.

**Architecture:** Phase 2 layers on top of the Phase 1 infrastructure (HTTP server, tool, registry, Mode C). New data layers (`ContactStore`, `PendingQueue`) manage persistent JSON files. The server's `handle_send_message` gains a three-way routing decision (unknown → approval, known → Mode A, trusted → Mode C with confidence evaluation). The tool gains eight new operations. mDNS discovery is a soft dependency via the `zeroconf` package. A `provider:request` hook injects pending approvals and messages into the active session.

**Tech Stack:** Python 3.11+, aiohttp (HTTP client/server), zeroconf (mDNS), amplifier-core (peer dependency), pytest + pytest-asyncio (testing)

**Design Doc:** `docs/plans/2026-02-22-a2a-bundle-phase2-design.md`

---

## Prerequisites

**Working directory:** `/home/bkrabach/dev/a2a-investigate/`

All paths in this plan are relative to `amplifier-bundle-a2a/` unless stated otherwise.

### Dev Environment Setup

The Phase 1 environment is already set up. Run this once before starting Phase 2 tasks:

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
source .venv/bin/activate

# Install zeroconf for mDNS discovery (Tasks 8-9)
pip install zeroconf
```

### Key API Reference (from amplifier-core)

Same as Phase 1, plus these Phase 2 additions:

| API | Signature | Notes |
|-----|-----------|-------|
| Tool protocol | `name: str`, `description: str`, `input_schema: dict`, `async execute(input: dict) -> ToolResult` | Duck-typed, no inheritance |
| Tool mount | `async def mount(coordinator, config=None)` | Register via `await coordinator.mount("tools", tool, name=tool.name)` |
| Hook mount | `async def mount(coordinator, config=None)` | Register handlers via `coordinator.hooks.register(event, handler, priority=N, name="...")` |
| Register capability | `coordinator.register_capability("a2a.registry", obj)` | NOT `coordinator.register()` |
| Get capability | `coordinator.get_capability("a2a.registry")` | Returns `None` if not registered |
| Register cleanup | `coordinator.register_cleanup(async_fn)` | Called in LIFO order on session end |
| ToolResult | `ToolResult(success=True, output=...)` or `ToolResult(success=False, error={"message": "..."})` | Import: `from amplifier_core import ToolResult` |
| HookResult | `HookResult(action="inject_context", context_injection="...", context_injection_role="user", ephemeral=True, suppress_output=True)` | For injecting pending messages/approvals |
| Provider access | `coordinator.mount_points["providers"]` | Returns list of registered providers |
| Provider complete | `await provider.complete(messages) -> response` | `messages` is `list[{"role": str, "content": str}]` |
| Hooks register | `coordinator.hooks.register("provider:request", handler, priority=5, name="a2a-pending-injection")` | For injecting context before LLM calls |

### Existing Test Conventions

- Tests use `pytest-asyncio` with `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed on async test methods inside classes)
- Tests use `from unittest.mock import AsyncMock, MagicMock, patch`
- Server tests use `from aiohttp.test_utils import TestClient as AioTestClient, TestServer as AioTestServer`
- `_make_mock_coordinator()` helper pattern for test setup
- Imports inside test methods (not at module level) for module-specific imports
- `assert result.output is not None` / `assert result.error is not None` type-narrowing guards before subscript access

### Run All Tests Command

After any task, verify no regressions:

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a && source .venv/bin/activate
pytest -v
```

Expected before Phase 2: 77 tests, all passing.

---

## Task 1: ContactStore (Data Layer)

A new `ContactStore` class that manages persistent contacts on disk at `~/.amplifier/a2a/contacts.json`. Pure data layer — CRUD operations, JSON persistence, asyncio.Lock for thread safety. No integration with server or registry yet.

**Files:**

- Create: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/contacts.py`
- Test: `modules/hooks-a2a-server/tests/test_contacts.py`

### Step 1: Write the failing tests

Create `modules/hooks-a2a-server/tests/test_contacts.py`:

```python
"""Tests for ContactStore — persistent contact management."""

import json


class TestContactStoreInit:
    """Tests for ContactStore initialization."""

    async def test_empty_store_has_no_contacts(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        assert await store.list_contacts() == []

    async def test_loads_existing_contacts_from_disk(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        # Pre-populate the contacts file
        contacts_file = tmp_path / "contacts.json"
        contacts_file.write_text(
            json.dumps(
                [
                    {
                        "url": "http://alice:8222",
                        "name": "Alice",
                        "tier": "trusted",
                        "first_seen": "2026-01-01T00:00:00Z",
                        "last_seen": "2026-01-01T00:00:00Z",
                    }
                ]
            )
        )

        store = ContactStore(base_dir=tmp_path)
        contacts = await store.list_contacts()
        assert len(contacts) == 1
        assert contacts[0]["name"] == "Alice"
        assert contacts[0]["tier"] == "trusted"

    async def test_handles_missing_file_gracefully(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        # No file exists — should not crash
        assert await store.list_contacts() == []

    async def test_handles_corrupt_file_gracefully(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        contacts_file = tmp_path / "contacts.json"
        contacts_file.write_text("not valid json!!!")

        store = ContactStore(base_dir=tmp_path)
        # Corrupt file — should start with empty list
        assert await store.list_contacts() == []


class TestContactStoreAddAndGet:
    """Tests for adding and retrieving contacts."""

    async def test_add_contact_default_tier(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice")

        contact = await store.get_contact("http://alice:8222")
        assert contact is not None
        assert contact["name"] == "Alice"
        assert contact["tier"] == "known"
        assert "first_seen" in contact
        assert "last_seen" in contact

    async def test_add_contact_with_explicit_tier(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice", tier="trusted")

        contact = await store.get_contact("http://alice:8222")
        assert contact is not None
        assert contact["tier"] == "trusted"

    async def test_get_contact_returns_none_for_unknown(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        assert await store.get_contact("http://nobody:8222") is None

    async def test_is_known_returns_true_for_existing_contact(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice")
        assert await store.is_known("http://alice:8222") is True

    async def test_is_known_returns_false_for_unknown_url(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        assert await store.is_known("http://nobody:8222") is False


class TestContactStoreUpdateAndRemove:
    """Tests for updating tiers and removing contacts."""

    async def test_update_tier(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice", tier="known")
        await store.update_tier("http://alice:8222", "trusted")

        contact = await store.get_contact("http://alice:8222")
        assert contact is not None
        assert contact["tier"] == "trusted"

    async def test_update_tier_unknown_url_is_noop(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        # Should not crash
        await store.update_tier("http://nobody:8222", "trusted")

    async def test_remove_contact(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice")
        await store.remove_contact("http://alice:8222")

        assert await store.get_contact("http://alice:8222") is None
        assert await store.is_known("http://alice:8222") is False

    async def test_remove_unknown_contact_is_noop(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        # Should not crash
        await store.remove_contact("http://nobody:8222")


class TestContactStorePersistence:
    """Tests for write-through persistence to disk."""

    async def test_add_contact_writes_to_disk(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice")

        # Read the file directly
        contacts_file = tmp_path / "contacts.json"
        assert contacts_file.exists()
        data = json.loads(contacts_file.read_text())
        assert len(data) == 1
        assert data[0]["url"] == "http://alice:8222"

    async def test_update_tier_writes_to_disk(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice", tier="known")
        await store.update_tier("http://alice:8222", "trusted")

        contacts_file = tmp_path / "contacts.json"
        data = json.loads(contacts_file.read_text())
        assert data[0]["tier"] == "trusted"

    async def test_remove_contact_writes_to_disk(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(base_dir=tmp_path)
        await store.add_contact("http://alice:8222", "Alice")
        await store.add_contact("http://bob:8222", "Bob")
        await store.remove_contact("http://alice:8222")

        contacts_file = tmp_path / "contacts.json"
        data = json.loads(contacts_file.read_text())
        assert len(data) == 1
        assert data[0]["url"] == "http://bob:8222"

    async def test_new_store_reads_previous_writes(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        # Write with first store
        store1 = ContactStore(base_dir=tmp_path)
        await store1.add_contact("http://alice:8222", "Alice", tier="trusted")

        # Read with second store
        store2 = ContactStore(base_dir=tmp_path)
        contact = await store2.get_contact("http://alice:8222")
        assert contact is not None
        assert contact["name"] == "Alice"
        assert contact["tier"] == "trusted"
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_contacts.py -v
```

Expected: FAIL — `ImportError: cannot import name 'ContactStore'`

### Step 3: Write the implementation

Create `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/contacts.py`:

```python
"""ContactStore — persistent contact management for A2A trust.

Manages contacts at {base_dir}/contacts.json with write-through persistence.
Thread-safe via asyncio.Lock since the server and hook may both access it.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_BASE_DIR = Path.home() / ".amplifier" / "a2a"


class ContactStore:
    """Persistent contact store backed by a JSON file.

    Each contact has: url, name, tier ("trusted" | "known"),
    first_seen, last_seen.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or _DEFAULT_BASE_DIR
        self._file = self._base_dir / "contacts.json"
        self._contacts: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        """Load contacts from disk. Handles missing/corrupt files."""
        if not self._file.exists():
            self._contacts = []
            return
        try:
            data = json.loads(self._file.read_text())
            if isinstance(data, list):
                self._contacts = data
            else:
                logger.warning("contacts.json is not a list, starting empty")
                self._contacts = []
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load contacts.json: %s", e)
            self._contacts = []

    def _save(self) -> None:
        """Write contacts to disk. Creates directory lazily."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(self._contacts, indent=2))

    async def get_contact(self, url: str) -> dict[str, Any] | None:
        """Look up a contact by URL."""
        async with self._lock:
            for contact in self._contacts:
                if contact["url"] == url:
                    return dict(contact)
            return None

    async def add_contact(
        self, url: str, name: str, tier: str = "known"
    ) -> None:
        """Add a new contact. Overwrites if URL already exists."""
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "url": url,
            "name": name,
            "tier": tier,
            "first_seen": now,
            "last_seen": now,
        }
        async with self._lock:
            # Remove existing entry for this URL if any
            self._contacts = [c for c in self._contacts if c["url"] != url]
            self._contacts.append(entry)
            self._save()

    async def update_tier(self, url: str, tier: str) -> None:
        """Update a contact's trust tier."""
        async with self._lock:
            for contact in self._contacts:
                if contact["url"] == url:
                    contact["tier"] = tier
                    self._save()
                    return

    async def remove_contact(self, url: str) -> None:
        """Remove a contact by URL."""
        async with self._lock:
            before = len(self._contacts)
            self._contacts = [c for c in self._contacts if c["url"] != url]
            if len(self._contacts) < before:
                self._save()

    async def list_contacts(self) -> list[dict[str, Any]]:
        """Return all contacts."""
        async with self._lock:
            return [dict(c) for c in self._contacts]

    async def is_known(self, url: str) -> bool:
        """Check if a URL is in the contact list."""
        async with self._lock:
            return any(c["url"] == url for c in self._contacts)
```

### Step 4: Run tests to verify they pass

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_contacts.py -v
```

Expected: All 16 tests PASS.

### Step 5: Run all tests to check for regressions

```bash
pytest -v
```

Expected: 93 tests (77 + 16), all passing.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/contacts.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_contacts.py
git commit -m "feat(a2a): add ContactStore for persistent contact management"
```

---

## Task 2: PendingQueue (Data Layer)

A new `PendingQueue` class that manages pending messages and pending approval requests on disk. Same persistence pattern as ContactStore — JSON file, write-through, asyncio.Lock. Two separate queues: `pending_messages.json` (Mode A) and `pending_approvals.json` (first-contact approval).

**Files:**

- Create: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py`
- Test: `modules/hooks-a2a-server/tests/test_pending.py`

### Step 1: Write the failing tests

Create `modules/hooks-a2a-server/tests/test_pending.py`:

```python
"""Tests for PendingQueue — pending messages and approvals."""

import json


class TestPendingMessages:
    """Tests for the pending messages queue (Mode A)."""

    async def test_empty_queue(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert await queue.list_pending_messages() == []

    async def test_add_and_list_pending_message(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben's Agent",
            message={"role": "user", "parts": [{"text": "What restaurant?"}]},
        )

        messages = await queue.list_pending_messages()
        assert len(messages) == 1
        assert messages[0]["task_id"] == "task-1"
        assert messages[0]["sender_url"] == "http://ben:8222"
        assert messages[0]["sender_name"] == "Ben's Agent"
        assert messages[0]["status"] == "pending"
        assert "received_at" in messages[0]

    async def test_get_pending_message_by_task_id(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        entry = await queue.get_pending_message("task-1")
        assert entry is not None
        assert entry["task_id"] == "task-1"

    async def test_get_pending_message_returns_none_for_unknown(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert await queue.get_pending_message("nonexistent") is None

    async def test_update_message_status(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        await queue.update_message_status("task-1", "responded")
        entry = await queue.get_pending_message("task-1")
        assert entry is not None
        assert entry["status"] == "responded"

    async def test_list_pending_messages_filters_by_status(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )
        await queue.add_pending_message(
            task_id="task-2",
            sender_url="http://alice:8222",
            sender_name="Alice",
            message={"role": "user", "parts": [{"text": "Hey"}]},
        )
        await queue.update_message_status("task-1", "responded")

        pending_only = await queue.list_pending_messages(status="pending")
        assert len(pending_only) == 1
        assert pending_only[0]["task_id"] == "task-2"

    async def test_pending_messages_persist_to_disk(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        messages_file = tmp_path / "pending_messages.json"
        assert messages_file.exists()
        data = json.loads(messages_file.read_text())
        assert len(data) == 1
        assert data[0]["task_id"] == "task-1"


class TestPendingApprovals:
    """Tests for the pending approvals queue (first-contact)."""

    async def test_empty_approvals(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert await queue.list_pending_approvals() == []

    async def test_add_and_list_pending_approval(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben's Agent",
            message={"role": "user", "parts": [{"text": "What restaurant?"}]},
        )

        approvals = await queue.list_pending_approvals()
        assert len(approvals) == 1
        assert approvals[0]["task_id"] == "task-1"
        assert approvals[0]["sender_url"] == "http://ben:8222"
        assert approvals[0]["status"] == "pending"

    async def test_get_pending_approval_by_sender_url(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        entry = await queue.get_pending_approval_by_url("http://ben:8222")
        assert entry is not None
        assert entry["sender_url"] == "http://ben:8222"

    async def test_get_pending_approval_returns_none_for_unknown(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        assert await queue.get_pending_approval_by_url("http://nobody:8222") is None

    async def test_update_approval_status(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        await queue.update_approval_status("http://ben:8222", "approved")
        entry = await queue.get_pending_approval_by_url("http://ben:8222")
        assert entry is not None
        assert entry["status"] == "approved"

    async def test_pending_approvals_persist_to_disk(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        approvals_file = tmp_path / "pending_approvals.json"
        assert approvals_file.exists()
        data = json.loads(approvals_file.read_text())
        assert len(data) == 1


class TestPendingQueueCorruptFiles:
    """Tests for graceful handling of corrupt files."""

    async def test_corrupt_messages_file_starts_empty(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        messages_file = tmp_path / "pending_messages.json"
        messages_file.write_text("corrupt!!!")

        queue = PendingQueue(base_dir=tmp_path)
        assert await queue.list_pending_messages() == []

    async def test_corrupt_approvals_file_starts_empty(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        approvals_file = tmp_path / "pending_approvals.json"
        approvals_file.write_text("{not a list}")

        queue = PendingQueue(base_dir=tmp_path)
        assert await queue.list_pending_approvals() == []
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_pending.py -v
```

Expected: FAIL — `ImportError: cannot import name 'PendingQueue'`

### Step 3: Write the implementation

Create `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py`:

```python
"""PendingQueue — manages pending messages and approval requests.

Two separate JSON files:
- pending_messages.json: Mode A messages waiting for user response
- pending_approvals.json: First-contact approval requests

Same persistence pattern as ContactStore — write-through, asyncio.Lock.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_BASE_DIR = Path.home() / ".amplifier" / "a2a"


class PendingQueue:
    """Manages pending messages and approval requests."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or _DEFAULT_BASE_DIR
        self._messages_file = self._base_dir / "pending_messages.json"
        self._approvals_file = self._base_dir / "pending_approvals.json"
        self._messages: list[dict[str, Any]] = []
        self._approvals: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        """Load both queues from disk."""
        self._messages = self._load_file(self._messages_file)
        self._approvals = self._load_file(self._approvals_file)

    @staticmethod
    def _load_file(path: Path) -> list[dict[str, Any]]:
        """Load a JSON list from a file. Returns [] on missing/corrupt."""
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
            logger.warning("%s is not a list, starting empty", path.name)
            return []
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load %s: %s", path.name, e)
            return []

    def _save_messages(self) -> None:
        """Write pending messages to disk."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._messages_file.write_text(json.dumps(self._messages, indent=2))

    def _save_approvals(self) -> None:
        """Write pending approvals to disk."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._approvals_file.write_text(json.dumps(self._approvals, indent=2))

    # --- Pending Messages (Mode A) ---

    async def add_pending_message(
        self,
        task_id: str,
        sender_url: str,
        sender_name: str,
        message: dict[str, Any],
    ) -> None:
        """Add a pending message entry."""
        entry = {
            "task_id": task_id,
            "sender_url": sender_url,
            "sender_name": sender_name,
            "message": message,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        async with self._lock:
            self._messages.append(entry)
            self._save_messages()

    async def get_pending_message(self, task_id: str) -> dict[str, Any] | None:
        """Get a pending message by task ID."""
        async with self._lock:
            for msg in self._messages:
                if msg["task_id"] == task_id:
                    return dict(msg)
            return None

    async def update_message_status(self, task_id: str, status: str) -> None:
        """Update a pending message's status."""
        async with self._lock:
            for msg in self._messages:
                if msg["task_id"] == task_id:
                    msg["status"] = status
                    self._save_messages()
                    return

    async def list_pending_messages(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List pending messages, optionally filtered by status."""
        async with self._lock:
            if status is None:
                return [dict(m) for m in self._messages]
            return [dict(m) for m in self._messages if m["status"] == status]

    # --- Pending Approvals (First-Contact) ---

    async def add_pending_approval(
        self,
        task_id: str,
        sender_url: str,
        sender_name: str,
        message: dict[str, Any],
    ) -> None:
        """Add a pending approval request."""
        entry = {
            "task_id": task_id,
            "sender_url": sender_url,
            "sender_name": sender_name,
            "message": message,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        async with self._lock:
            self._approvals.append(entry)
            self._save_approvals()

    async def get_pending_approval_by_url(
        self, sender_url: str
    ) -> dict[str, Any] | None:
        """Get a pending approval by sender URL (most recent pending one)."""
        async with self._lock:
            for approval in reversed(self._approvals):
                if (
                    approval["sender_url"] == sender_url
                    and approval["status"] == "pending"
                ):
                    return dict(approval)
            return None

    async def update_approval_status(
        self, sender_url: str, status: str
    ) -> None:
        """Update the most recent pending approval for a sender URL."""
        async with self._lock:
            for approval in reversed(self._approvals):
                if (
                    approval["sender_url"] == sender_url
                    and approval["status"] == "pending"
                ):
                    approval["status"] = status
                    self._save_approvals()
                    return

    async def list_pending_approvals(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List pending approvals, optionally filtered by status."""
        async with self._lock:
            if status is None:
                return [dict(a) for a in self._approvals]
            return [dict(a) for a in self._approvals if a["status"] == status]
```

### Step 4: Run tests to verify they pass

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_pending.py -v
```

Expected: All 16 tests PASS.

### Step 5: Run all tests to check for regressions

```bash
pytest -v
```

Expected: 109 tests (93 + 16), all passing.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_pending.py
git commit -m "feat(a2a): add PendingQueue for messages and approval requests"
```

---

## Task 3: Integrate ContactStore and PendingQueue into Registry and Mount

Wire `ContactStore` and `PendingQueue` into the existing `A2ARegistry` and create them in `mount()`. No behavior change — just plumbing so later tasks can access them.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py`
- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`
- Modify: `modules/hooks-a2a-server/tests/test_registry.py` (add tests)
- Modify: `modules/hooks-a2a-server/tests/test_server.py` (update mount tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_registry.py`:

```python
class TestContactStoreAndPendingQueue:
    """Tests for contact_store and pending_queue on registry."""

    def test_registry_has_contact_store_attribute(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        assert registry.contact_store is None

    def test_registry_has_pending_queue_attribute(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        assert registry.pending_queue is None

    def test_set_and_get_contact_store(self):
        from unittest.mock import MagicMock
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        mock_store = MagicMock()
        registry.contact_store = mock_store
        assert registry.contact_store is mock_store

    def test_set_and_get_pending_queue(self):
        from unittest.mock import MagicMock
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        mock_queue = MagicMock()
        registry.pending_queue = mock_queue
        assert registry.pending_queue is mock_queue
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_registry.py::TestContactStoreAndPendingQueue -v
```

Expected: FAIL — `AttributeError: 'A2ARegistry' object has no attribute 'contact_store'`

### Step 3: Update the registry implementation

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py`, add two attributes to `A2ARegistry.__init__`. Find this section:

```python
        self._tasks: dict[str, _Task] = {}
        self._card_cache: dict[str, _CachedCard] = {}
```

Replace with:

```python
        self._tasks: dict[str, _Task] = {}
        self._card_cache: dict[str, _CachedCard] = {}
        self.contact_store: Any | None = None
        self.pending_queue: Any | None = None
```

### Step 4: Run the registry tests

```bash
pytest modules/hooks-a2a-server/tests/test_registry.py -v
```

Expected: All 20 tests PASS (16 original + 4 new).

### Step 5: Update mount() to create ContactStore and PendingQueue

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`, replace the entire file contents with:

```python
"""A2A server hook module — receives messages from remote agents.

Starts an HTTP server in mount() to serve the Agent Card and handle
incoming A2A messages. Registers the shared A2ARegistry as a coordinator
capability for tool-a2a to access.
"""

import logging
from typing import Any

__amplifier_module_type__ = "hook"

logger = logging.getLogger(__name__)


async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> None:
    """Mount the A2A server.

    - Skips in child sessions (parent_id guard)
    - Creates the A2ARegistry and registers it as a capability
    - Creates ContactStore and PendingQueue, wires into registry
    - Starts the HTTP server
    - Registers cleanup for graceful shutdown
    """
    config = config or {}

    # Don't start the server in child sessions (e.g., Mode C spawned sessions)
    if coordinator.parent_id:
        logger.debug("Skipping A2A server in child session %s", coordinator.session_id)
        return

    from .card import build_agent_card
    from .contacts import ContactStore
    from .pending import PendingQueue
    from .registry import A2ARegistry
    from .server import A2AServer

    # Build shared state from config
    known_agents = config.get("known_agents", [])
    registry = A2ARegistry(known_agents=known_agents)

    # Create data stores and wire into registry
    contact_store = ContactStore()
    pending_queue = PendingQueue()
    registry.contact_store = contact_store
    registry.pending_queue = pending_queue

    # Build the Agent Card
    card = build_agent_card(config)

    # Register shared state so tool-a2a can access it
    coordinator.register_capability("a2a.registry", registry)

    # Create and start the HTTP server
    server = A2AServer(registry, card, coordinator, config)
    try:
        await server.start()
    except OSError as e:
        logger.warning("A2A server failed to start (port conflict?): %s", e)
        return

    # Register cleanup for graceful shutdown
    coordinator.register_cleanup(server.stop)
```

### Step 6: Update mount test to verify new wiring

In `modules/hooks-a2a-server/tests/test_server.py`, find the test `test_mount_registers_capability_and_cleanup` in class `TestMount` and replace it with:

```python
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

        # Verify contact_store and pending_queue are wired
        assert registry.contact_store is not None
        assert registry.pending_queue is not None

        # Verify cleanup was registered
        coordinator.register_cleanup.assert_called_once()

        # Clean up: call the cleanup function
        cleanup_fn = coordinator.register_cleanup.call_args[0][0]
        await cleanup_fn()
```

### Step 7: Run all tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest -v
```

Expected: All 113 tests PASS (109 + 4 new registry tests).

### Step 8: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): integrate ContactStore and PendingQueue into registry and mount"
```

---

## Task 4: First-Contact Approval — Server Side

Modify `handle_send_message` to check `contact_store.is_known(sender_url)` before processing. Unknown senders get queued as approval requests with `INPUT_REQUIRED` status. The request body now requires a `sender_url` field.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`
- Test: `modules/hooks-a2a-server/tests/test_approval.py` (create)

### Step 1: Write the failing tests

Create `modules/hooks-a2a-server/tests/test_approval.py`:

```python
"""Tests for first-contact approval flow — server side."""

from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient as AioTestClient, TestServer as AioTestServer

from amplifier_module_hooks_a2a_server.registry import A2ARegistry
from amplifier_module_hooks_a2a_server.card import build_agent_card


def _make_mock_coordinator():
    coordinator = MagicMock()
    coordinator.session_id = "parent-session-123"
    coordinator.parent_id = None
    coordinator.config = {
        "session": {"orchestrator": "loop-basic", "context": "context-simple"},
        "providers": [{"module": "provider-test", "config": {"model": "test-model"}}],
        "tools": [{"module": "tool-filesystem"}],
        "hooks": [{"module": "hooks-a2a-server", "config": {"port": 8222}}],
    }
    return coordinator


def _make_mock_session(response_text="The answer is 42"):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=response_text)
    mock_session.initialize = AsyncMock()
    mock_session.cleanup = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


async def _make_server_with_contacts(tmp_path, contacts=None):
    """Create a server with a real ContactStore and PendingQueue."""
    from amplifier_module_hooks_a2a_server.contacts import ContactStore
    from amplifier_module_hooks_a2a_server.pending import PendingQueue
    from amplifier_module_hooks_a2a_server.server import A2AServer

    config = {
        "port": 0,
        "agent_name": "Test Agent",
        "agent_description": "Test",
    }
    registry = A2ARegistry()
    card = build_agent_card(config)

    contact_store = ContactStore(base_dir=tmp_path)
    pending_queue = PendingQueue(base_dir=tmp_path)
    registry.contact_store = contact_store
    registry.pending_queue = pending_queue

    # Pre-populate contacts if provided
    if contacts:
        for c in contacts:
            await contact_store.add_contact(c["url"], c["name"], tier=c.get("tier", "known"))

    coordinator = _make_mock_coordinator()
    server = A2AServer(registry, card, coordinator, config)
    return server, registry, contact_store, pending_queue


class TestUnknownSenderApproval:
    async def test_unknown_sender_gets_input_required(self, tmp_path):
        server, registry, _, _ = await _make_server_with_contacts(tmp_path)

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hello"}]},
                    "sender_url": "http://unknown:8222",
                    "sender_name": "Unknown Agent",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "INPUT_REQUIRED"

    async def test_unknown_sender_queued_in_pending_approvals(self, tmp_path):
        server, _, _, pending_queue = await _make_server_with_contacts(tmp_path)

        async with AioTestClient(AioTestServer(server.app)) as client:
            await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hello"}]},
                    "sender_url": "http://unknown:8222",
                    "sender_name": "Unknown Agent",
                },
            )

        approvals = await pending_queue.list_pending_approvals()
        assert len(approvals) == 1
        assert approvals[0]["sender_url"] == "http://unknown:8222"
        assert approvals[0]["sender_name"] == "Unknown Agent"
        assert approvals[0]["status"] == "pending"

    async def test_missing_sender_url_returns_400(self, tmp_path):
        server, _, _, _ = await _make_server_with_contacts(tmp_path)

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hello"}]},
                    # No sender_url
                },
            )
            assert resp.status == 400

    async def test_known_sender_proceeds_to_mode_c(self, tmp_path):
        contacts = [{"url": "http://trusted:8222", "name": "Trusted", "tier": "trusted"}]
        server, _, _, _ = await _make_server_with_contacts(tmp_path, contacts=contacts)
        mock_session = _make_mock_session("Mode C response")

        with patch(
            "amplifier_module_hooks_a2a_server.server.AmplifierSession",
            return_value=mock_session,
        ):
            async with AioTestClient(AioTestServer(server.app)) as client:
                resp = await client.post(
                    "/a2a/v1/message:send",
                    json={
                        "message": {"role": "user", "parts": [{"text": "Hi"}]},
                        "sender_url": "http://trusted:8222",
                        "sender_name": "Trusted",
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "COMPLETED"

    async def test_approval_task_is_pollable(self, tmp_path):
        server, registry, _, _ = await _make_server_with_contacts(tmp_path)

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hello"}]},
                    "sender_url": "http://unknown:8222",
                    "sender_name": "Unknown",
                },
            )
            data = await resp.json()
            task_id = data["id"]

            # Poll the task
            poll_resp = await client.get(f"/a2a/v1/tasks/{task_id}")
            poll_data = await poll_resp.json()
            assert poll_data["status"] == "INPUT_REQUIRED"
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_approval.py -v
```

Expected: FAIL — tests fail because `handle_send_message` doesn't check contacts yet.

### Step 3: Update server.py with contact checking

Replace the contents of `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`:

```python
"""A2A HTTP server — handles incoming requests from remote agents."""

import logging
from typing import Any

from aiohttp import web
from amplifier_core.session import AmplifierSession

logger = logging.getLogger(__name__)


class A2AServer:
    """HTTP server that serves the Agent Card and handles A2A messages.

    Routes:
        GET  /.well-known/agent.json   — Agent Card (identity document)
        POST /a2a/v1/message:send      — Receive a message
        GET  /a2a/v1/tasks/{task_id}   — Poll task status
    """

    def __init__(
        self,
        registry: Any,
        card: dict[str, Any],
        coordinator: Any,
        config: dict[str, Any],
    ) -> None:
        self.registry = registry
        self.card = card
        self.coordinator = coordinator
        self.config = config
        self.port: int | None = None

        self.app = web.Application()
        self.app.router.add_get("/.well-known/agent.json", self.handle_agent_card)
        self.app.router.add_post("/a2a/v1/message:send", self.handle_send_message)
        self.app.router.add_get("/a2a/v1/tasks/{task_id}", self.handle_get_task)

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start the HTTP server."""
        port = self.config.get("port", 8222)
        host = self.config.get("host", "0.0.0.0")

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        self._runner = runner
        self._site = site

        # Resolve actual port (important when port=0 for OS assignment)
        sockets = site._server.sockets  # type: ignore[union-attr]
        if sockets:
            self.port = sockets[0].getsockname()[1]
        else:
            self.port = port

        logger.info("A2A server started on %s:%s", host, self.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("A2A server stopped")

    # --- Route handlers ---

    async def handle_agent_card(self, request: web.Request) -> web.Response:
        """GET /.well-known/agent.json — serve the Agent Card."""
        return web.json_response(self.card)

    async def handle_send_message(self, request: web.Request) -> web.Response:
        """POST /a2a/v1/message:send — receive a message from a remote agent.

        Phase 2 routing:
        1. Check sender_url is present
        2. If sender is unknown → queue for approval (INPUT_REQUIRED)
        3. If sender is known (tier="known") → Mode A (queue for user, Task 10)
        4. If sender is trusted (tier="trusted") → Mode C (spawn child session)
        """
        # Parse and validate request
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        message = body.get("message")
        if not message or not isinstance(message.get("parts"), list):
            return web.json_response(
                {"error": "Missing or invalid 'message' with 'parts'"},
                status=400,
            )

        # Extract text from message parts
        text_parts = [
            part["text"]
            for part in message.get("parts", [])
            if isinstance(part, dict) and "text" in part
        ]
        text = " ".join(text_parts).strip()
        if not text:
            return web.json_response({"error": "Empty message text"}, status=400)

        # Phase 2: require sender_url
        sender_url = body.get("sender_url", "").strip()
        if not sender_url:
            return web.json_response(
                {"error": "Missing 'sender_url' in request body"},
                status=400,
            )
        sender_name = body.get("sender_name", "Unknown Agent")

        # Check contact status
        contact_store = self.registry.contact_store
        if contact_store and not await contact_store.is_known(sender_url):
            # Unknown sender → queue for approval
            return await self._handle_unknown_sender(
                sender_url, sender_name, message, text
            )

        # Known contact — determine tier
        contact = None
        if contact_store:
            contact = await contact_store.get_contact(sender_url)
        tier = contact["tier"] if contact else "trusted"

        if tier == "known":
            # Mode A: queue for user response (implemented in Task 10)
            # For now, fall through to Mode C
            pass

        # Mode C: spawn child session to answer (trusted tier, or fallback)
        return await self._handle_mode_c(message, text)

    async def _handle_unknown_sender(
        self,
        sender_url: str,
        sender_name: str,
        message: dict[str, Any],
        text: str,
    ) -> web.Response:
        """Handle a message from an unknown sender — queue for approval."""
        task_id = self.registry.create_task(message)
        self.registry.update_task(task_id, "INPUT_REQUIRED")

        pending_queue = self.registry.pending_queue
        if pending_queue:
            await pending_queue.add_pending_approval(
                task_id=task_id,
                sender_url=sender_url,
                sender_name=sender_name,
                message=message,
            )

        return web.json_response(self.registry.get_task(task_id))

    async def _handle_mode_c(
        self, message: dict[str, Any], text: str
    ) -> web.Response:
        """Handle Mode C — spawn child session to answer autonomously."""
        task_id = self.registry.create_task(message)
        self.registry.update_task(task_id, "WORKING")

        try:
            response_text = await self._spawn_child_session(task_id, text)
            artifact = {"parts": [{"text": response_text}]}
            self.registry.update_task(task_id, "COMPLETED", artifacts=[artifact])
            return web.json_response(self.registry.get_task(task_id))
        except Exception as e:
            logger.exception("Mode C failed for task %s", task_id)
            self.registry.update_task(task_id, "FAILED", error=str(e))
            return web.json_response(self.registry.get_task(task_id), status=500)

    async def _spawn_child_session(self, task_id: str, prompt: str) -> str:
        """Spawn a child AmplifierSession to answer a prompt.

        Builds a child config from the parent (same providers and tools,
        but NO hooks to avoid recursive server starts). Returns the
        child session's response text.
        """
        parent_config = self.coordinator.config

        # Child gets same providers and tools, but no hooks
        child_config = {
            "session": dict(parent_config.get("session", {})),
            "providers": list(parent_config.get("providers", [])),
            "tools": list(parent_config.get("tools", [])),
            # Deliberately omit "hooks" — child doesn't need a2a server
        }

        child_session_id = f"{self.coordinator.session_id}-a2a-{task_id[:8]}"

        child = AmplifierSession(
            config=child_config,
            session_id=child_session_id,
            parent_id=self.coordinator.session_id,
        )

        try:
            async with child:
                return await child.execute(prompt)
        except Exception as e:
            raise RuntimeError(f"Child session failed: {e}") from e

    async def handle_get_task(self, request: web.Request) -> web.Response:
        """GET /a2a/v1/tasks/{task_id} — poll task status."""
        task_id = request.match_info["task_id"]
        task = self.registry.get_task(task_id)
        if task is None:
            return web.json_response(
                {"error": f"Task not found: {task_id}"}, status=404
            )
        return web.json_response(task)
```

### Step 4: Update existing Mode C tests for sender_url

The existing tests in `test_mode_c.py` need to include `sender_url` in their POST bodies. But first, we need the existing `test_mode_c.py` to pass when `contact_store` is `None` (which it is in those tests since they use a plain `A2ARegistry`). The code above falls through to Mode C when `contact_store` is `None`, so the old tests should still work **if** they include `sender_url`. However, the new validation requires `sender_url`, so we need to update the existing test payloads.

In `modules/hooks-a2a-server/tests/test_mode_c.py`, find **every** `json={"message":` POST call and add `"sender_url": "http://sender:8222"` to the payload. There are 5 POST calls that need updating. For each one, change:

```python
                    json={
                        "message": {
                            "role": "user",
                            "parts": [{"text": "..."}],
                        }
                    },
```

to:

```python
                    json={
                        "message": {
                            "role": "user",
                            "parts": [{"text": "..."}],
                        },
                        "sender_url": "http://sender:8222",
                    },
```

The specific tests to update are:
1. `test_spawns_session_and_returns_completed_task` — add `"sender_url": "http://sender:8222"` to the JSON body
2. `test_child_session_gets_parent_id` — same
3. `test_child_session_excludes_hooks` — same
4. `test_returns_500_when_session_fails` — same
5. `test_task_stored_in_registry` — same

The tests `test_rejects_invalid_json`, `test_rejects_missing_message`, and `test_rejects_empty_message_parts` don't need `sender_url` because they fail at JSON validation before the sender check.

Also update `tests/test_integration.py`: the `test_send_message_and_receive_response` and `test_tool_send_through_real_server` tests send via `A2AClient.send_message()`, which builds the payload without `sender_url`. We need to update `A2AClient.send_message()` to include `sender_url` in its payload. **Do this in Task 13** when we refactor the client for async send. For now, integration tests will fail because the client doesn't send `sender_url`. Fix this by updating the client payload temporarily:

In `modules/tool-a2a/amplifier_module_tool_a2a/client.py`, find the `send_message` method's payload construction:

```python
        payload = {
            "message": {
                "role": "user",
                "parts": [{"text": message_text}],
            }
        }
```

Replace with:

```python
        payload = {
            "message": {
                "role": "user",
                "parts": [{"text": message_text}],
            },
            "sender_url": sender_url or "",
            "sender_name": sender_name or "",
        }
```

And update the `send_message` method signature from:

```python
    async def send_message(
        self,
        base_url: str,
        message_text: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
```

to:

```python
    async def send_message(
        self,
        base_url: str,
        message_text: str,
        timeout: float | None = None,
        sender_url: str | None = None,
        sender_name: str | None = None,
    ) -> dict[str, Any]:
```

And in the `_op_send` method in `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, find:

```python
        # Send the message
        result = await self.client.send_message(url, message, timeout=timeout)
```

Replace with:

```python
        # Determine our own URL to include as sender_url
        our_url = ""
        if self.registry and hasattr(self.registry, "contact_store"):
            # Use the server's base URL if available from the card
            pass  # Will be populated from server config in future
        # Send the message
        result = await self.client.send_message(
            url, message, timeout=timeout, sender_url=our_url
        )
```

### Step 5: Run all tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest -v
```

Expected: All tests PASS (previous tests + 5 new approval tests).

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git add amplifier-bundle-a2a/modules/tool-a2a/
git add amplifier-bundle-a2a/tests/
git commit -m "feat(a2a): add first-contact approval flow — server side"
```

---

## Task 5: First-Contact Approval — Tool Operations

Add `approve`, `block`, `contacts`, and `trust` operations to `tool-a2a`. Approve adds to contacts and reprocesses the queued message. Block rejects the task.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/tests/test_tool_a2a.py` (add tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/tool-a2a/tests/test_tool_a2a.py`:

```python
class TestApproveOperation:
    async def test_approve_adds_contact_and_succeeds(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        # Mock contact_store
        registry.contact_store = AsyncMock()
        registry.contact_store.add_contact = AsyncMock()
        # Mock pending_queue with a pending approval
        registry.pending_queue = AsyncMock()
        registry.pending_queue.get_pending_approval_by_url = AsyncMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://ben:8222",
                "sender_name": "Ben",
                "message": {"role": "user", "parts": [{"text": "Hi"}]},
                "status": "pending",
            }
        )
        registry.pending_queue.update_approval_status = AsyncMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "approve", "agent": "http://ben:8222"}
        )
        assert result.success is True
        registry.contact_store.add_contact.assert_called_once()
        registry.pending_queue.update_approval_status.assert_called_once()

    async def test_approve_requires_agent(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "approve"})
        assert result.success is False
        assert result.error is not None
        assert "required" in result.error["message"].lower()

    async def test_approve_no_pending_approval_returns_error(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.contact_store = AsyncMock()
        registry.pending_queue = AsyncMock()
        registry.pending_queue.get_pending_approval_by_url = AsyncMock(
            return_value=None
        )
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "approve", "agent": "http://nobody:8222"}
        )
        assert result.success is False


class TestBlockOperation:
    async def test_block_rejects_and_updates_task(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = AsyncMock()
        registry.pending_queue.get_pending_approval_by_url = AsyncMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://ben:8222",
                "sender_name": "Ben",
                "message": {"role": "user", "parts": [{"text": "Hi"}]},
                "status": "pending",
            }
        )
        registry.pending_queue.update_approval_status = AsyncMock()
        registry.update_task = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "block", "agent": "http://ben:8222"}
        )
        assert result.success is True
        registry.pending_queue.update_approval_status.assert_called_once()
        registry.update_task.assert_called_once_with("task-1", "REJECTED")

    async def test_block_requires_agent(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "block"})
        assert result.success is False


class TestContactsOperation:
    async def test_contacts_lists_all_contacts(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.contact_store = AsyncMock()
        registry.contact_store.list_contacts = AsyncMock(
            return_value=[
                {"url": "http://alice:8222", "name": "Alice", "tier": "trusted"},
                {"url": "http://bob:8222", "name": "Bob", "tier": "known"},
            ]
        )
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "contacts"})
        assert result.success is True
        assert result.output is not None
        assert len(result.output) == 2

    async def test_contacts_empty_list(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.contact_store = AsyncMock()
        registry.contact_store.list_contacts = AsyncMock(return_value=[])
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "contacts"})
        assert result.success is True


class TestTrustOperation:
    async def test_trust_updates_tier(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.contact_store = AsyncMock()
        registry.contact_store.get_contact = AsyncMock(
            return_value={"url": "http://alice:8222", "name": "Alice", "tier": "known"}
        )
        registry.contact_store.update_tier = AsyncMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "trust", "agent": "http://alice:8222", "tier": "trusted"}
        )
        assert result.success is True
        registry.contact_store.update_tier.assert_called_once_with(
            "http://alice:8222", "trusted"
        )

    async def test_trust_requires_agent(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "trust"})
        assert result.success is False

    async def test_trust_unknown_contact_returns_error(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.contact_store = AsyncMock()
        registry.contact_store.get_contact = AsyncMock(return_value=None)
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "trust", "agent": "http://nobody:8222", "tier": "trusted"}
        )
        assert result.success is False
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestApproveOperation -v
```

Expected: FAIL — `Unknown operation: approve`

### Step 3: Add the new operations to A2ATool

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, update the class:

1. Update `description` property to include new operations.

2. Update `input_schema` property — add new enum values and new properties:

In the `input_schema` property, change:
```python
                "operation": {
                    "type": "string",
                    "enum": ["agents", "card", "send"],
                    "description": "The operation to perform",
                },
```
to:
```python
                "operation": {
                    "type": "string",
                    "enum": [
                        "agents", "card", "send",
                        "approve", "block", "contacts", "trust",
                    ],
                    "description": "The operation to perform",
                },
```

And add a new `tier` property to the schema properties:
```python
                "tier": {
                    "type": "string",
                    "enum": ["trusted", "known"],
                    "description": "Trust tier for 'approve' and 'trust' operations",
                },
```

3. Add dispatch in `execute` method — after the `elif operation == "send"` block, add:

```python
            elif operation == "approve":
                return await self._op_approve(input)
            elif operation == "block":
                return await self._op_block(input)
            elif operation == "contacts":
                return await self._op_contacts()
            elif operation == "trust":
                return await self._op_trust(input)
```

4. Add the new operation methods:

```python
    async def _op_approve(self, input: dict[str, Any]) -> ToolResult:
        """Approve a pending first-contact request."""
        agent = input.get("agent", "").strip()
        if not agent:
            return ToolResult(
                success=False,
                error={"message": "Agent URL required"},
            )

        if not self.registry or not hasattr(self.registry, "pending_queue"):
            return ToolResult(
                success=False,
                error={"message": "A2A registry not available"},
            )

        pending_queue = self.registry.pending_queue
        approval = await pending_queue.get_pending_approval_by_url(agent)
        if not approval:
            return ToolResult(
                success=False,
                error={"message": f"No pending approval for {agent}"},
            )

        # Add to contacts
        tier = input.get("tier", "known")
        contact_store = self.registry.contact_store
        await contact_store.add_contact(
            agent, approval["sender_name"], tier=tier
        )

        # Mark approval as approved
        await pending_queue.update_approval_status(agent, "approved")

        return ToolResult(
            success=True,
            output=f"Approved {approval['sender_name']} ({agent}) as '{tier}'. "
            f"Their message will be processed.",
        )

    async def _op_block(self, input: dict[str, Any]) -> ToolResult:
        """Block a pending first-contact request."""
        agent = input.get("agent", "").strip()
        if not agent:
            return ToolResult(
                success=False,
                error={"message": "Agent URL required"},
            )

        if not self.registry or not hasattr(self.registry, "pending_queue"):
            return ToolResult(
                success=False,
                error={"message": "A2A registry not available"},
            )

        pending_queue = self.registry.pending_queue
        approval = await pending_queue.get_pending_approval_by_url(agent)
        if not approval:
            return ToolResult(
                success=False,
                error={"message": f"No pending approval for {agent}"},
            )

        # Reject the task
        self.registry.update_task(approval["task_id"], "REJECTED")

        # Mark approval as blocked
        await pending_queue.update_approval_status(agent, "blocked")

        return ToolResult(
            success=True,
            output=f"Blocked {approval['sender_name']} ({agent}). "
            f"Their request has been rejected.",
        )

    async def _op_contacts(self) -> ToolResult:
        """List all contacts with their trust tiers."""
        if not self.registry or not hasattr(self.registry, "contact_store"):
            return ToolResult(
                success=False,
                error={"message": "A2A registry not available"},
            )

        contact_store = self.registry.contact_store
        contacts = await contact_store.list_contacts()
        if not contacts:
            return ToolResult(
                success=True,
                output="No contacts yet. Agents are added when you approve "
                "first-contact requests.",
            )
        return ToolResult(success=True, output=contacts)

    async def _op_trust(self, input: dict[str, Any]) -> ToolResult:
        """Update a contact's trust tier."""
        agent = input.get("agent", "").strip()
        if not agent:
            return ToolResult(
                success=False,
                error={"message": "Agent URL required"},
            )

        tier = input.get("tier", "trusted")

        if not self.registry or not hasattr(self.registry, "contact_store"):
            return ToolResult(
                success=False,
                error={"message": "A2A registry not available"},
            )

        contact_store = self.registry.contact_store
        contact = await contact_store.get_contact(agent)
        if not contact:
            return ToolResult(
                success=False,
                error={"message": f"No contact found for {agent}"},
            )

        await contact_store.update_tier(agent, tier)
        return ToolResult(
            success=True,
            output=f"Updated {contact['name']} ({agent}) to tier '{tier}'.",
        )
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py -v
```

Expected: All tests PASS (20 original + 10 new).

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/
git commit -m "feat(a2a): add approve, block, contacts, and trust tool operations"
```

---

## Task 6: Approval Injection into Active Session

Register a `provider:request` hook handler that checks for pending approvals and injects them via `HookResult`. This is the notification mechanism that tells the user about pending approval requests.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`
- Test: `modules/hooks-a2a-server/tests/test_approval.py` (add tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_approval.py`:

```python
class TestApprovalInjection:
    async def test_injects_pending_approvals(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben's Agent",
            message={"role": "user", "parts": [{"text": "What restaurant?"}]},
        )

        # Import the handler function
        from amplifier_module_hooks_a2a_server import _make_injection_handler

        handler = _make_injection_handler(queue)
        result = await handler({})

        assert result is not None
        assert result.action == "inject_context"
        assert "Ben's Agent" in result.context_injection
        assert "approve" in result.context_injection
        assert result.ephemeral is True

    async def test_no_injection_when_no_pending(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)

        from amplifier_module_hooks_a2a_server import _make_injection_handler

        handler = _make_injection_handler(queue)
        result = await handler({})

        assert result is None

    async def test_injection_only_fires_once(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben's Agent",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        from amplifier_module_hooks_a2a_server import _make_injection_handler

        handler = _make_injection_handler(queue)

        # First call: should inject
        result1 = await handler({})
        assert result1 is not None

        # Second call: should NOT inject (one-time flag)
        result2 = await handler({})
        assert result2 is None
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_approval.py::TestApprovalInjection -v
```

Expected: FAIL — `ImportError: cannot import name '_make_injection_handler'`

### Step 3: Add the injection handler to __init__.py

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`, add the following at the end of the file (after the `mount` function), and also register the hook in `mount()`:

First, add the handler factory function at module level (after `mount`):

```python
def _make_injection_handler(pending_queue: Any) -> Any:
    """Create a provider:request hook handler that injects pending approvals/messages.

    Returns a handler function. Uses a closure to track the one-time injection flag.
    """
    injected = {"approvals": False, "messages": False}

    async def handler(event_data: Any) -> Any:
        from amplifier_core import HookResult

        parts = []

        # Check for pending approvals (one-time per session)
        if not injected["approvals"]:
            approvals = await pending_queue.list_pending_approvals(status="pending")
            if approvals:
                injected["approvals"] = True
                lines = ["<a2a-approval-request>"]
                for a in approvals:
                    lines.append(
                        f'New agent requesting access: "{a["sender_name"]}" '
                        f'({a["sender_url"]})'
                    )
                    # Extract message text
                    msg_text = ""
                    msg = a.get("message", {})
                    for part in msg.get("parts", []):
                        if isinstance(part, dict) and "text" in part:
                            msg_text += part["text"]
                    if msg_text:
                        lines.append(f'Message: "{msg_text}"')
                    lines.append(
                        f'Use a2a(operation="approve", agent="{a["sender_url"]}") '
                        f"to allow,"
                    )
                    lines.append(
                        f'or a2a(operation="block", agent="{a["sender_url"]}") '
                        f"to block."
                    )
                    lines.append("")
                lines.append("</a2a-approval-request>")
                parts.append("\n".join(lines))

        # Check for pending messages (one-time per session) — Task 11 will add this

        if not parts:
            return None

        return HookResult(
            action="inject_context",
            context_injection="\n\n".join(parts),
            context_injection_role="user",
            ephemeral=True,
            suppress_output=True,
        )

    return handler
```

Then, in the `mount()` function, after `coordinator.register_capability("a2a.registry", registry)` and before creating the server, add:

```python
    # Register provider:request hook for pending injection
    injection_handler = _make_injection_handler(pending_queue)
    coordinator.hooks.register(
        "provider:request",
        injection_handler,
        priority=5,
        name="a2a-pending-injection",
    )
```

Update the `mount` function's `coordinator` mock expectations — add `coordinator.hooks = MagicMock()` to the `_make_mock_coordinator` in test_server.py.

In `modules/hooks-a2a-server/tests/test_server.py`, find the `_make_mock_coordinator` function and add:

```python
    coordinator.hooks = MagicMock()
    coordinator.hooks.register = MagicMock()
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_approval.py -v
```

Expected: All approval tests PASS.

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): add approval injection via provider:request hook"
```

---

## Task 7: Per-Contact Capability Scoping

Modify `_spawn_child_session` to accept a `tier` parameter and filter the tools list based on the tier's whitelist. Trusted gets all tools, known gets a configurable subset.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`
- Modify: `modules/hooks-a2a-server/tests/test_mode_c.py` (add tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_mode_c.py`:

```python
class TestCapabilityScoping:
    async def test_trusted_tier_gets_all_tools(self):
        mock_session = _make_mock_session("OK")
        coordinator = _make_mock_coordinator()
        # Parent has multiple tools
        coordinator.config["tools"] = [
            {"module": "tool-filesystem"},
            {"module": "tool-bash"},
            {"module": "tool-search"},
        ]
        server, _ = _make_server(coordinator)

        with patch(
            "amplifier_module_hooks_a2a_server.server.AmplifierSession",
            return_value=mock_session,
        ) as mock_cls:
            async with AioTestClient(AioTestServer(server.app)) as client:
                await client.post(
                    "/a2a/v1/message:send",
                    json={
                        "message": {"role": "user", "parts": [{"text": "Hi"}]},
                        "sender_url": "http://sender:8222",
                    },
                )
                # Trusted tier (default) should get all tools
                call_kwargs = mock_cls.call_args[1]
                child_config = call_kwargs["config"]
                assert len(child_config["tools"]) == 3

    async def test_known_tier_gets_filtered_tools(self):
        from amplifier_module_hooks_a2a_server.server import A2AServer

        mock_session = _make_mock_session("OK")
        coordinator = _make_mock_coordinator()
        coordinator.config["tools"] = [
            {"module": "tool-filesystem"},
            {"module": "tool-bash"},
            {"module": "tool-search"},
            {"module": "tool-web"},
        ]

        config = {
            "port": 0,
            "agent_name": "Test",
            "trust_tiers": {
                "known": {"tools": ["tool-filesystem", "tool-search"]},
            },
        }
        registry = A2ARegistry()
        card = build_agent_card(config)
        server = A2AServer(registry, card, coordinator, config)

        with patch(
            "amplifier_module_hooks_a2a_server.server.AmplifierSession",
            return_value=mock_session,
        ) as mock_cls:
            # Call _spawn_child_session directly with tier="known"
            await server._spawn_child_session("test-task", "Hi", tier="known")

            call_kwargs = mock_cls.call_args[1]
            child_config = call_kwargs["config"]
            tool_modules = [t["module"] for t in child_config["tools"]]
            assert "tool-filesystem" in tool_modules
            assert "tool-search" in tool_modules
            assert "tool-bash" not in tool_modules
            assert "tool-web" not in tool_modules

    async def test_known_tier_default_whitelist(self):
        from amplifier_module_hooks_a2a_server.server import A2AServer

        mock_session = _make_mock_session("OK")
        coordinator = _make_mock_coordinator()
        coordinator.config["tools"] = [
            {"module": "tool-filesystem"},
            {"module": "tool-bash"},
            {"module": "tool-search"},
        ]

        config = {
            "port": 0,
            "agent_name": "Test",
            # No trust_tiers config — should use defaults
        }
        registry = A2ARegistry()
        card = build_agent_card(config)
        server = A2AServer(registry, card, coordinator, config)

        with patch(
            "amplifier_module_hooks_a2a_server.server.AmplifierSession",
            return_value=mock_session,
        ) as mock_cls:
            await server._spawn_child_session("test-task", "Hi", tier="known")

            call_kwargs = mock_cls.call_args[1]
            child_config = call_kwargs["config"]
            tool_modules = [t["module"] for t in child_config["tools"]]
            # Default known whitelist: tool-filesystem, tool-search
            assert "tool-filesystem" in tool_modules
            assert "tool-search" in tool_modules
            assert "tool-bash" not in tool_modules
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_mode_c.py::TestCapabilityScoping -v
```

Expected: FAIL — `_spawn_child_session() got an unexpected keyword argument 'tier'`

### Step 3: Update _spawn_child_session to support tier filtering

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`, replace the `_spawn_child_session` method:

```python
    _DEFAULT_KNOWN_TOOLS = ["tool-filesystem", "tool-search"]

    async def _spawn_child_session(
        self, task_id: str, prompt: str, tier: str = "trusted"
    ) -> str:
        """Spawn a child AmplifierSession to answer a prompt.

        Builds a child config from the parent (same providers and tools,
        but NO hooks to avoid recursive server starts). Filters tools
        based on the sender's trust tier.
        """
        parent_config = self.coordinator.config

        # Filter tools based on tier
        tools = list(parent_config.get("tools", []))
        if tier != "trusted":
            # Get the whitelist for this tier
            trust_tiers = self.config.get("trust_tiers", {})
            tier_config = trust_tiers.get(tier, {})
            allowed_tools = tier_config.get("tools", self._DEFAULT_KNOWN_TOOLS)

            if allowed_tools != "*":
                tools = [
                    t for t in tools if t.get("module") in allowed_tools
                ]

        child_config = {
            "session": dict(parent_config.get("session", {})),
            "providers": list(parent_config.get("providers", [])),
            "tools": tools,
            # Deliberately omit "hooks" — child doesn't need a2a server
        }

        child_session_id = f"{self.coordinator.session_id}-a2a-{task_id[:8]}"

        child = AmplifierSession(
            config=child_config,
            session_id=child_session_id,
            parent_id=self.coordinator.session_id,
        )

        try:
            async with child:
                return await child.execute(prompt)
        except Exception as e:
            raise RuntimeError(f"Child session failed: {e}") from e
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_mode_c.py -v
```

Expected: All tests PASS (8 original + 3 new).

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): add per-contact capability scoping by trust tier"
```

---

## Task 8: mDNS Advertisement (Server Side)

Create `discovery.py` in the hooks module. Register a Zeroconf service when the server starts, unregister on cleanup. Graceful degradation if zeroconf is unavailable.

**Files:**

- Create: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/discovery.py`
- Test: `modules/hooks-a2a-server/tests/test_discovery.py`
- Modify: `modules/hooks-a2a-server/pyproject.toml` (add zeroconf dependency)

### Step 1: Add zeroconf dependency

In `modules/hooks-a2a-server/pyproject.toml`, change:

```toml
dependencies = ["aiohttp>=3.9.0"]
```

to:

```toml
dependencies = ["aiohttp>=3.9.0", "zeroconf>=0.131.0"]
```

### Step 2: Write the failing tests

Create `modules/hooks-a2a-server/tests/test_discovery.py`:

```python
"""Tests for mDNS advertisement — server side."""

from unittest.mock import MagicMock, patch, AsyncMock


class TestMDNSAdvertiser:
    async def test_register_service(self):
        from amplifier_module_hooks_a2a_server.discovery import MDNSAdvertiser

        with patch(
            "amplifier_module_hooks_a2a_server.discovery.Zeroconf"
        ) as mock_zc_cls:
            mock_zc = MagicMock()
            mock_zc_cls.return_value = mock_zc

            advertiser = MDNSAdvertiser(
                agent_name="Test Agent",
                port=8222,
                base_url="http://test.local:8222",
            )
            await advertiser.register()

            mock_zc.register_service.assert_called_once()
            # Verify the service info was created correctly
            call_args = mock_zc.register_service.call_args[0]
            service_info = call_args[0]
            assert service_info.name.startswith("Test Agent")
            assert service_info.port == 8222

    async def test_unregister_service(self):
        from amplifier_module_hooks_a2a_server.discovery import MDNSAdvertiser

        with patch(
            "amplifier_module_hooks_a2a_server.discovery.Zeroconf"
        ) as mock_zc_cls:
            mock_zc = MagicMock()
            mock_zc_cls.return_value = mock_zc

            advertiser = MDNSAdvertiser(
                agent_name="Test Agent",
                port=8222,
                base_url="http://test.local:8222",
            )
            await advertiser.register()
            await advertiser.unregister()

            mock_zc.unregister_service.assert_called_once()
            mock_zc.close.assert_called_once()

    async def test_unregister_without_register_is_noop(self):
        from amplifier_module_hooks_a2a_server.discovery import MDNSAdvertiser

        advertiser = MDNSAdvertiser(
            agent_name="Test",
            port=8222,
            base_url="http://test:8222",
        )
        # Should not crash
        await advertiser.unregister()


class TestMDNSGracefulDegradation:
    async def test_register_handles_import_error(self):
        """If zeroconf is not installed, registration silently fails."""
        from amplifier_module_hooks_a2a_server.discovery import MDNSAdvertiser

        with patch(
            "amplifier_module_hooks_a2a_server.discovery.Zeroconf",
            side_effect=Exception("No zeroconf"),
        ):
            advertiser = MDNSAdvertiser(
                agent_name="Test",
                port=8222,
                base_url="http://test:8222",
            )
            # Should not raise
            await advertiser.register()

    async def test_register_handles_network_error(self):
        """If no network interfaces, registration silently fails."""
        from amplifier_module_hooks_a2a_server.discovery import MDNSAdvertiser

        with patch(
            "amplifier_module_hooks_a2a_server.discovery.Zeroconf"
        ) as mock_zc_cls:
            mock_zc = MagicMock()
            mock_zc.register_service.side_effect = OSError("No network")
            mock_zc_cls.return_value = mock_zc

            advertiser = MDNSAdvertiser(
                agent_name="Test",
                port=8222,
                base_url="http://test:8222",
            )
            # Should not raise
            await advertiser.register()
```

### Step 3: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_discovery.py -v
```

Expected: FAIL — `ImportError: cannot import name 'MDNSAdvertiser'`

### Step 4: Write the implementation

Create `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/discovery.py`:

```python
"""mDNS advertisement — registers the A2A server on the local network.

Uses Zeroconf to advertise the agent as a _a2a._tcp.local. service.
Gracefully degrades if zeroconf is unavailable or no network interfaces.
"""

import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

try:
    from zeroconf import ServiceInfo, Zeroconf

    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False
    Zeroconf = None  # type: ignore[misc,assignment]
    ServiceInfo = None  # type: ignore[misc,assignment]

_SERVICE_TYPE = "_a2a._tcp.local."


class MDNSAdvertiser:
    """Advertises an A2A agent via mDNS/Zeroconf."""

    def __init__(
        self,
        agent_name: str,
        port: int,
        base_url: str,
    ) -> None:
        self.agent_name = agent_name
        self.port = port
        self.base_url = base_url
        self._zeroconf: Any | None = None
        self._service_info: Any | None = None

    async def register(self) -> None:
        """Register the mDNS service. Silently fails on error."""
        if not _ZEROCONF_AVAILABLE:
            logger.info("Zeroconf not installed — mDNS discovery disabled")
            return

        try:
            self._zeroconf = Zeroconf()

            # Build TXT record properties
            properties = {
                b"name": self.agent_name.encode(),
                b"version": b"1.0",
                b"url": self.base_url.encode(),
            }

            service_name = f"{self.agent_name}.{_SERVICE_TYPE}"
            self._service_info = ServiceInfo(
                _SERVICE_TYPE,
                service_name,
                addresses=[socket.inet_aton("0.0.0.0")],
                port=self.port,
                properties=properties,
            )

            self._zeroconf.register_service(self._service_info)
            logger.info("mDNS: registered %s on port %d", service_name, self.port)

        except Exception as e:
            logger.warning("mDNS registration failed (continuing without): %s", e)
            self._zeroconf = None
            self._service_info = None

    async def unregister(self) -> None:
        """Unregister the mDNS service and close Zeroconf."""
        if self._zeroconf is None:
            return

        try:
            if self._service_info:
                self._zeroconf.unregister_service(self._service_info)
            self._zeroconf.close()
            logger.info("mDNS: unregistered service")
        except Exception as e:
            logger.warning("mDNS unregistration failed: %s", e)
        finally:
            self._zeroconf = None
            self._service_info = None
```

### Step 5: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_discovery.py -v
```

Expected: All 5 tests PASS.

### Step 6: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): add mDNS/Zeroconf advertisement for server discovery"
```

---

## Task 9: mDNS Browsing (Tool Side)

Create `discovery.py` in the tool module. New `discover` operation browses for `_a2a._tcp.local.` services. Cache results in the registry. Update `agents` operation to merge config + mDNS + contacts sources.

**Files:**

- Create: `modules/tool-a2a/amplifier_module_tool_a2a/discovery.py`
- Test: `modules/tool-a2a/tests/test_discovery.py` (create)
- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/pyproject.toml` (add zeroconf dependency)

### Step 1: Add zeroconf dependency

In `modules/tool-a2a/pyproject.toml`, change:

```toml
dependencies = ["aiohttp>=3.9.0"]
```

to:

```toml
dependencies = ["aiohttp>=3.9.0", "zeroconf>=0.131.0"]
```

### Step 2: Write the failing tests

Create `modules/tool-a2a/tests/test_discovery.py`:

```python
"""Tests for mDNS browsing — tool side."""

import asyncio
from unittest.mock import MagicMock, patch


class TestMDNSBrowser:
    async def test_browse_returns_discovered_agents(self):
        from amplifier_module_tool_a2a.discovery import browse_mdns

        # Mock Zeroconf and ServiceBrowser
        with patch("amplifier_module_tool_a2a.discovery.Zeroconf") as mock_zc_cls, \
             patch("amplifier_module_tool_a2a.discovery.ServiceBrowser") as mock_sb_cls:

            mock_zc = MagicMock()
            mock_zc_cls.return_value = mock_zc

            # Simulate service info
            mock_info = MagicMock()
            mock_info.name = "Alice Agent._a2a._tcp.local."
            mock_info.port = 8222
            mock_info.properties = {
                b"name": b"Alice Agent",
                b"url": b"http://alice.local:8222",
            }
            mock_info.parsed_addresses.return_value = ["192.168.1.10"]
            mock_zc.get_service_info.return_value = mock_info

            # Mock the ServiceBrowser to call the add handler
            def fake_browser(zc, service_type, handlers):
                # Simulate finding a service
                handlers.add_service(zc, service_type, "Alice Agent._a2a._tcp.local.")

            mock_sb_cls.side_effect = fake_browser

            results = await browse_mdns(timeout=0.1)

            assert len(results) == 1
            assert results[0]["name"] == "Alice Agent"
            assert results[0]["url"] == "http://alice.local:8222"
            assert results[0]["source"] == "mdns"

            mock_zc.close.assert_called_once()

    async def test_browse_returns_empty_when_no_services(self):
        from amplifier_module_tool_a2a.discovery import browse_mdns

        with patch("amplifier_module_tool_a2a.discovery.Zeroconf") as mock_zc_cls, \
             patch("amplifier_module_tool_a2a.discovery.ServiceBrowser"):

            mock_zc = MagicMock()
            mock_zc_cls.return_value = mock_zc

            results = await browse_mdns(timeout=0.1)
            assert results == []

    async def test_browse_handles_import_error(self):
        from amplifier_module_tool_a2a.discovery import browse_mdns

        with patch(
            "amplifier_module_tool_a2a.discovery.Zeroconf",
            side_effect=Exception("No zeroconf"),
        ):
            results = await browse_mdns(timeout=0.1)
            assert results == []
```

### Step 3: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_discovery.py -v
```

Expected: FAIL — `ImportError: cannot import name 'browse_mdns'`

### Step 4: Write the implementation

Create `modules/tool-a2a/amplifier_module_tool_a2a/discovery.py`:

```python
"""mDNS browsing — discovers A2A agents on the local network.

Uses Zeroconf ServiceBrowser to find _a2a._tcp.local. services.
Gracefully degrades if zeroconf is unavailable.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False
    Zeroconf = None  # type: ignore[misc,assignment]
    ServiceBrowser = None  # type: ignore[misc,assignment]

_SERVICE_TYPE = "_a2a._tcp.local."


async def browse_mdns(timeout: float = 2.0) -> list[dict[str, Any]]:
    """Browse for A2A agents via mDNS. Returns a list of discovered agents.

    Each entry: {"name": str, "url": str, "source": "mdns"}

    Returns empty list if zeroconf is unavailable or browsing fails.
    """
    if not _ZEROCONF_AVAILABLE:
        logger.info("Zeroconf not installed — mDNS browsing disabled")
        return []

    discovered: list[dict[str, Any]] = []

    try:
        zc = Zeroconf()
    except Exception as e:
        logger.warning("mDNS browsing failed to initialize: %s", e)
        return []

    try:
        service_names: list[str] = []

        class _Handlers:
            @staticmethod
            def add_service(zc_instance: Any, service_type: str, name: str) -> None:
                service_names.append(name)

            @staticmethod
            def remove_service(zc_instance: Any, service_type: str, name: str) -> None:
                pass

            @staticmethod
            def update_service(zc_instance: Any, service_type: str, name: str) -> None:
                pass

        handlers = _Handlers()
        ServiceBrowser(zc, _SERVICE_TYPE, handlers)

        # Wait for services to be discovered
        await asyncio.sleep(timeout)

        # Resolve each discovered service
        for name in service_names:
            info = zc.get_service_info(_SERVICE_TYPE, name)
            if info is None:
                continue

            props = info.properties or {}
            agent_name = props.get(b"name", b"").decode("utf-8", errors="replace")
            url = props.get(b"url", b"").decode("utf-8", errors="replace")

            if not url and info.port:
                addresses = info.parsed_addresses()
                if addresses:
                    url = f"http://{addresses[0]}:{info.port}"

            if agent_name and url:
                discovered.append({
                    "name": agent_name,
                    "url": url,
                    "source": "mdns",
                })

    except Exception as e:
        logger.warning("mDNS browsing failed: %s", e)
    finally:
        zc.close()

    return discovered
```

### Step 5: Add `discover` operation and update `agents` operation in the tool

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`:

1. Update `input_schema` enum to include `"discover"`:

```python
                    "enum": [
                        "agents", "card", "send",
                        "approve", "block", "contacts", "trust",
                        "discover",
                    ],
```

2. Add dispatch in `execute`:

```python
            elif operation == "discover":
                return await self._op_discover(input)
```

3. Add the operation method:

```python
    async def _op_discover(self, input: dict[str, Any]) -> ToolResult:
        """Discover A2A agents on the local network via mDNS."""
        from .discovery import browse_mdns

        timeout = input.get("timeout", 2.0)
        agents = await browse_mdns(timeout=timeout)

        # Cache in registry if available
        if self.registry and hasattr(self.registry, "_discovery_cache"):
            self.registry._discovery_cache = agents

        if not agents:
            return ToolResult(
                success=True,
                output="No agents discovered on the local network.",
            )
        return ToolResult(success=True, output=agents)
```

4. Update the `_op_agents` method to merge sources:

Replace the current `_op_agents` method with:

```python
    async def _op_agents(self) -> ToolResult:
        """List all known remote agents (config + mDNS + contacts)."""
        if not self.registry:
            return ToolResult(
                success=False,
                error={
                    "message": (
                        "A2A registry not available. "
                        "Is the hooks-a2a-server module loaded?"
                    )
                },
            )

        all_agents: dict[str, dict[str, Any]] = {}  # keyed by URL

        # Source 1: configured known agents
        for agent in self.registry.get_agents():
            all_agents[agent["url"]] = {
                **agent,
                "source": "config",
            }

        # Source 2: mDNS discovery cache
        if hasattr(self.registry, "_discovery_cache"):
            for agent in getattr(self.registry, "_discovery_cache", []):
                url = agent["url"]
                if url in all_agents:
                    all_agents[url]["source"] = "both"
                else:
                    all_agents[url] = dict(agent)

        # Source 3: contacts
        if hasattr(self.registry, "contact_store") and self.registry.contact_store:
            contacts = await self.registry.contact_store.list_contacts()
            for contact in contacts:
                url = contact["url"]
                if url in all_agents:
                    all_agents[url]["tier"] = contact["tier"]
                else:
                    all_agents[url] = {
                        "name": contact["name"],
                        "url": url,
                        "source": "contact",
                        "tier": contact["tier"],
                    }

        agents_list = list(all_agents.values())
        if not agents_list:
            return ToolResult(
                success=True,
                output="No known agents. Use a2a(operation=\"discover\") to find "
                "agents on the local network, or configure known_agents in settings.",
            )
        return ToolResult(success=True, output=agents_list)
```

### Step 6: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/ -v
```

Expected: All tests PASS.

### Step 7: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 8: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/
git commit -m "feat(a2a): add mDNS browsing and discover operation"
```

---

## Task 10: Mode A — Server-Side Flow

Modify `handle_send_message` to route "known" tier contacts to Mode A: queue the message to `PendingQueue`, return `INPUT_REQUIRED` immediately.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`
- Test: `modules/hooks-a2a-server/tests/test_mode_a.py` (create)

### Step 1: Write the failing tests

Create `modules/hooks-a2a-server/tests/test_mode_a.py`:

```python
"""Tests for Mode A — notify-and-wait server-side flow."""

from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient as AioTestClient, TestServer as AioTestServer

from amplifier_module_hooks_a2a_server.registry import A2ARegistry
from amplifier_module_hooks_a2a_server.card import build_agent_card


async def _make_server_with_contacts(tmp_path, contacts=None):
    """Create a server with real data stores."""
    from amplifier_module_hooks_a2a_server.contacts import ContactStore
    from amplifier_module_hooks_a2a_server.pending import PendingQueue
    from amplifier_module_hooks_a2a_server.server import A2AServer

    config = {"port": 0, "agent_name": "Test"}
    registry = A2ARegistry()
    card = build_agent_card(config)

    contact_store = ContactStore(base_dir=tmp_path)
    pending_queue = PendingQueue(base_dir=tmp_path)
    registry.contact_store = contact_store
    registry.pending_queue = pending_queue

    if contacts:
        for c in contacts:
            await contact_store.add_contact(
                c["url"], c["name"], tier=c.get("tier", "known")
            )

    coordinator = MagicMock()
    coordinator.session_id = "parent-session"
    coordinator.parent_id = None
    coordinator.config = {
        "session": {"orchestrator": "loop-basic", "context": "context-simple"},
        "providers": [{"module": "provider-test"}],
        "tools": [{"module": "tool-filesystem"}],
    }

    server = A2AServer(registry, card, coordinator, config)
    return server, registry, pending_queue


class TestModeARouting:
    async def test_known_contact_routes_to_mode_a(self, tmp_path):
        contacts = [{"url": "http://ben:8222", "name": "Ben", "tier": "known"}]
        server, _, pending_queue = await _make_server_with_contacts(
            tmp_path, contacts=contacts
        )

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "What restaurant?"}]},
                    "sender_url": "http://ben:8222",
                    "sender_name": "Ben",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "INPUT_REQUIRED"

    async def test_mode_a_queues_pending_message(self, tmp_path):
        contacts = [{"url": "http://ben:8222", "name": "Ben", "tier": "known"}]
        server, _, pending_queue = await _make_server_with_contacts(
            tmp_path, contacts=contacts
        )

        async with AioTestClient(AioTestServer(server.app)) as client:
            await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "What restaurant?"}]},
                    "sender_url": "http://ben:8222",
                    "sender_name": "Ben",
                },
            )

        messages = await pending_queue.list_pending_messages()
        assert len(messages) == 1
        assert messages[0]["sender_url"] == "http://ben:8222"
        assert messages[0]["status"] == "pending"

    async def test_trusted_contact_routes_to_mode_c(self, tmp_path):
        contacts = [{"url": "http://alice:8222", "name": "Alice", "tier": "trusted"}]
        server, _, _ = await _make_server_with_contacts(
            tmp_path, contacts=contacts
        )
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value="Mode C answer")
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "amplifier_module_hooks_a2a_server.server.AmplifierSession",
            return_value=mock_session,
        ):
            async with AioTestClient(AioTestServer(server.app)) as client:
                resp = await client.post(
                    "/a2a/v1/message:send",
                    json={
                        "message": {"role": "user", "parts": [{"text": "Hi"}]},
                        "sender_url": "http://alice:8222",
                        "sender_name": "Alice",
                    },
                )
                data = await resp.json()
                assert data["status"] == "COMPLETED"

    async def test_mode_a_task_is_pollable(self, tmp_path):
        contacts = [{"url": "http://ben:8222", "name": "Ben", "tier": "known"}]
        server, _, _ = await _make_server_with_contacts(
            tmp_path, contacts=contacts
        )

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hi"}]},
                    "sender_url": "http://ben:8222",
                    "sender_name": "Ben",
                },
            )
            data = await resp.json()
            task_id = data["id"]

            poll_resp = await client.get(f"/a2a/v1/tasks/{task_id}")
            poll_data = await poll_resp.json()
            assert poll_data["status"] == "INPUT_REQUIRED"
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_mode_a.py -v
```

Expected: FAIL — known tier falls through to Mode C instead of Mode A.

### Step 3: Update handle_send_message for Mode A routing

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`, find this section in `handle_send_message`:

```python
        if tier == "known":
            # Mode A: queue for user response (implemented in Task 10)
            # For now, fall through to Mode C
            pass
```

Replace with:

```python
        if tier == "known":
            # Mode A: queue for user response
            return await self._handle_mode_a(
                sender_url, sender_name, message, text
            )
```

And add the `_handle_mode_a` method to the `A2AServer` class:

```python
    async def _handle_mode_a(
        self,
        sender_url: str,
        sender_name: str,
        message: dict[str, Any],
        text: str,
    ) -> web.Response:
        """Handle Mode A — queue message for user response."""
        task_id = self.registry.create_task(message)
        self.registry.update_task(task_id, "INPUT_REQUIRED")

        pending_queue = self.registry.pending_queue
        if pending_queue:
            await pending_queue.add_pending_message(
                task_id=task_id,
                sender_url=sender_url,
                sender_name=sender_name,
                message=message,
            )

        return web.json_response(self.registry.get_task(task_id))
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_mode_a.py -v
```

Expected: All 4 tests PASS.

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): add Mode A server-side flow for known contacts"
```

---

## Task 11: Mode A — Pending Message Injection

Extend the `provider:request` hook handler (from Task 6) to also inject pending messages, not just approvals. One-time injection flag per session.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`
- Modify: `modules/hooks-a2a-server/tests/test_approval.py` (add tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_approval.py`:

```python
class TestMessageInjection:
    async def test_injects_pending_messages(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben's Agent",
            message={"role": "user", "parts": [{"text": "What restaurant?"}]},
        )

        from amplifier_module_hooks_a2a_server import _make_injection_handler

        handler = _make_injection_handler(queue)
        result = await handler({})

        assert result is not None
        assert result.action == "inject_context"
        assert "Ben's Agent" in result.context_injection
        assert "respond" in result.context_injection
        assert "task-1" in result.context_injection

    async def test_injects_both_approvals_and_messages(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_approval(
            task_id="approval-1",
            sender_url="http://new:8222",
            sender_name="New Agent",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )
        await queue.add_pending_message(
            task_id="msg-1",
            sender_url="http://ben:8222",
            sender_name="Ben's Agent",
            message={"role": "user", "parts": [{"text": "What restaurant?"}]},
        )

        from amplifier_module_hooks_a2a_server import _make_injection_handler

        handler = _make_injection_handler(queue)
        result = await handler({})

        assert result is not None
        # Both approval and message sections should be present
        assert "a2a-approval-request" in result.context_injection
        assert "a2a-pending-messages" in result.context_injection

    async def test_message_injection_fires_once(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_pending_message(
            task_id="task-1",
            sender_url="http://ben:8222",
            sender_name="Ben",
            message={"role": "user", "parts": [{"text": "Hi"}]},
        )

        from amplifier_module_hooks_a2a_server import _make_injection_handler

        handler = _make_injection_handler(queue)

        result1 = await handler({})
        assert result1 is not None

        result2 = await handler({})
        assert result2 is None
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_approval.py::TestMessageInjection -v
```

Expected: FAIL — the handler doesn't yet inject pending messages.

### Step 3: Update the injection handler

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`, update the `_make_injection_handler` function's `handler` inner function. Find the comment `# Check for pending messages (one-time per session) — Task 11 will add this` and replace it with:

```python
        # Check for pending messages (one-time per session)
        if not injected["messages"]:
            messages = await pending_queue.list_pending_messages(status="pending")
            if messages:
                injected["messages"] = True
                lines = ["<a2a-pending-messages>"]
                lines.append(
                    f"You have {len(messages)} pending message(s) "
                    f"from remote agent(s):"
                )
                lines.append("")
                for m in messages:
                    lines.append(
                        f'From: {m["sender_name"]} ({m["sender_url"]})'
                    )
                    lines.append(f'Received: {m["received_at"]}')
                    msg_text = ""
                    msg = m.get("message", {})
                    for part in msg.get("parts", []):
                        if isinstance(part, dict) and "text" in part:
                            msg_text += part["text"]
                    if msg_text:
                        lines.append(f'Message: "{msg_text}"')
                    lines.append(
                        f'Use a2a(operation="respond", task_id="{m["task_id"]}", '
                        f'message="your reply") to respond.'
                    )
                    lines.append(
                        f'Or a2a(operation="dismiss", task_id="{m["task_id"]}") '
                        f"to dismiss."
                    )
                    lines.append("")
                lines.append("</a2a-pending-messages>")
                parts.append("\n".join(lines))
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_approval.py -v
```

Expected: All tests PASS.

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): add pending message injection via provider:request hook"
```

---

## Task 12: Mode A — Respond and Dismiss Operations

Add `respond` and `dismiss` operations to `tool-a2a`. Respond updates the task to COMPLETED with the user's reply as artifact. Dismiss sets task to REJECTED.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/tests/test_tool_a2a.py` (add tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/tool-a2a/tests/test_tool_a2a.py`:

```python
class TestRespondOperation:
    async def test_respond_updates_task_to_completed(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = AsyncMock()
        registry.pending_queue.get_pending_message = AsyncMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://ben:8222",
                "sender_name": "Ben",
                "message": {"role": "user", "parts": [{"text": "What restaurant?"}]},
                "status": "pending",
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.update_task = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "respond", "task_id": "task-1", "message": "Italian place"}
        )
        assert result.success is True
        registry.update_task.assert_called_once()
        call_args = registry.update_task.call_args
        assert call_args[0][0] == "task-1"
        assert call_args[0][1] == "COMPLETED"
        # Verify artifact
        artifacts = call_args[1].get("artifacts") or call_args[0][2]
        assert artifacts[0]["parts"][0]["text"] == "Italian place"

    async def test_respond_requires_task_id(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "respond", "message": "reply"}
        )
        assert result.success is False

    async def test_respond_requires_message(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "respond", "task_id": "task-1"}
        )
        assert result.success is False

    async def test_respond_unknown_task_returns_error(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = AsyncMock()
        registry.pending_queue.get_pending_message = AsyncMock(return_value=None)
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "respond", "task_id": "no-such-task", "message": "reply"}
        )
        assert result.success is False


class TestDismissOperation:
    async def test_dismiss_rejects_task(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = AsyncMock()
        registry.pending_queue.get_pending_message = AsyncMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://ben:8222",
                "sender_name": "Ben",
                "status": "pending",
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.update_task = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "dismiss", "task_id": "task-1"}
        )
        assert result.success is True
        registry.update_task.assert_called_once_with("task-1", "REJECTED")
        registry.pending_queue.update_message_status.assert_called_once_with(
            "task-1", "dismissed"
        )

    async def test_dismiss_requires_task_id(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "dismiss"})
        assert result.success is False
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestRespondOperation -v
```

Expected: FAIL — `Unknown operation: respond`

### Step 3: Add respond and dismiss operations

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`:

1. Update `input_schema` enum:

```python
                    "enum": [
                        "agents", "card", "send",
                        "approve", "block", "contacts", "trust",
                        "discover", "respond", "dismiss",
                    ],
```

2. Add `task_id` to input_schema properties:

```python
                "task_id": {
                    "type": "string",
                    "description": "Task ID for 'respond', 'dismiss', and 'status' operations",
                },
```

3. Add dispatch in `execute`:

```python
            elif operation == "respond":
                return await self._op_respond(input)
            elif operation == "dismiss":
                return await self._op_dismiss(input)
```

4. Add the operation methods:

```python
    async def _op_respond(self, input: dict[str, Any]) -> ToolResult:
        """Respond to a pending message from a remote agent."""
        task_id = input.get("task_id", "").strip()
        if not task_id:
            return ToolResult(
                success=False,
                error={"message": "task_id required"},
            )

        message = input.get("message", "").strip()
        if not message:
            return ToolResult(
                success=False,
                error={"message": "Message text required"},
            )

        if not self.registry or not hasattr(self.registry, "pending_queue"):
            return ToolResult(
                success=False,
                error={"message": "A2A registry not available"},
            )

        pending_queue = self.registry.pending_queue
        entry = await pending_queue.get_pending_message(task_id)
        if not entry:
            return ToolResult(
                success=False,
                error={"message": f"No pending message for task {task_id}"},
            )

        # Update task with response artifact
        artifact = {"parts": [{"text": message}]}
        self.registry.update_task(task_id, "COMPLETED", artifacts=[artifact])

        # Mark as responded
        await pending_queue.update_message_status(task_id, "responded")

        return ToolResult(
            success=True,
            output=f"Response sent to {entry['sender_name']} ({entry['sender_url']}). "
            f"They will receive it on their next poll.",
        )

    async def _op_dismiss(self, input: dict[str, Any]) -> ToolResult:
        """Dismiss a pending message (reject it)."""
        task_id = input.get("task_id", "").strip()
        if not task_id:
            return ToolResult(
                success=False,
                error={"message": "task_id required"},
            )

        if not self.registry or not hasattr(self.registry, "pending_queue"):
            return ToolResult(
                success=False,
                error={"message": "A2A registry not available"},
            )

        pending_queue = self.registry.pending_queue
        entry = await pending_queue.get_pending_message(task_id)
        if not entry:
            return ToolResult(
                success=False,
                error={"message": f"No pending message for task {task_id}"},
            )

        self.registry.update_task(task_id, "REJECTED")
        await pending_queue.update_message_status(task_id, "dismissed")

        return ToolResult(
            success=True,
            output=f"Dismissed message from {entry['sender_name']}.",
        )
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py -v
```

Expected: All tests PASS.

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/
git commit -m "feat(a2a): add respond and dismiss operations for Mode A"
```

---

## Task 13: Async Send and Status Polling

Modify `_op_send` to support `blocking=false` (return task handle immediately). Add client-side polling loop for `blocking=true` (poll in 1-second intervals). Add `status` operation.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/client.py`
- Modify: `modules/tool-a2a/tests/test_tool_a2a.py` (add tests)
- Modify: `modules/tool-a2a/tests/test_client.py` (add tests)

### Step 1: Write the failing tests

Add to the **end** of `modules/tool-a2a/tests/test_tool_a2a.py`:

```python
class TestAsyncSend:
    async def test_send_blocking_false_returns_immediately(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.resolve_agent_url = MagicMock(return_value="http://alice:8222")
        registry.get_cached_card = MagicMock(return_value={"name": "Alice"})
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        tool.client = MagicMock()
        tool.client.send_message = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "SUBMITTED",
                "history": [],
                "artifacts": [],
            }
        )

        result = await tool.execute(
            {
                "operation": "send",
                "agent": "Alice",
                "message": "Hi",
                "blocking": False,
            }
        )
        assert result.success is True
        assert result.output is not None
        assert result.output["status"] == "SUBMITTED"
        # Should NOT poll — just return immediately
        tool.client.send_message.assert_called_once()


class TestStatusOperation:
    async def test_status_returns_task_state(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.resolve_agent_url = MagicMock(return_value="http://alice:8222")
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        tool.client = MagicMock()
        tool.client.get_task_status = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "COMPLETED",
                "artifacts": [{"parts": [{"text": "Done"}]}],
            }
        )

        result = await tool.execute(
            {"operation": "status", "agent": "Alice", "task_id": "task-1"}
        )
        assert result.success is True
        assert result.output is not None
        assert result.output["status"] == "COMPLETED"

    async def test_status_requires_agent(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "status", "task_id": "task-1"}
        )
        assert result.success is False

    async def test_status_requires_task_id(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "status", "agent": "Alice"}
        )
        assert result.success is False
```

Add to `modules/tool-a2a/tests/test_client.py`:

```python
class TestSendMessageWithSenderInfo:
    async def test_includes_sender_url_in_payload(self):
        from amplifier_module_tool_a2a.client import A2AClient

        app = _make_mock_a2a_server()

        # Override handler to capture the payload
        captured = {}

        async def capture_send(request):
            body = await request.json()
            captured.update(body)
            return web.json_response(
                {"id": "t1", "status": "COMPLETED", "artifacts": [], "history": []}
            )

        app.router._resources = []  # Clear routes
        app.router.add_get("/.well-known/agent.json", lambda r: web.json_response({"name": "test"}))
        app.router.add_post("/a2a/v1/message:send", capture_send)
        app.router.add_get("/a2a/v1/tasks/{task_id}", lambda r: web.json_response({"id": "t", "status": "COMPLETED"}))

        async with AioTestClient(AioTestServer(app)) as mock:
            base_url = str(mock.make_url(""))
            client = A2AClient(timeout=5.0)
            try:
                await client.send_message(
                    base_url, "Hi",
                    sender_url="http://me:8222",
                    sender_name="My Agent",
                )
                assert captured.get("sender_url") == "http://me:8222"
                assert captured.get("sender_name") == "My Agent"
            finally:
                await client.close()
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestStatusOperation -v
```

Expected: FAIL — `Unknown operation: status`

### Step 3: Add status operation and async send support

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`:

1. Update `input_schema` enum to include `"status"`:

```python
                    "enum": [
                        "agents", "card", "send",
                        "approve", "block", "contacts", "trust",
                        "discover", "respond", "dismiss", "status",
                    ],
```

2. Add `blocking` to the schema properties:

```python
                "blocking": {
                    "type": "boolean",
                    "description": "If false, send returns immediately without waiting (default: true)",
                },
```

3. Add dispatch in `execute`:

```python
            elif operation == "status":
                return await self._op_status(input)
```

4. Update `_op_send` to handle `blocking` parameter. Replace the existing `_op_send` method:

```python
    async def _op_send(self, input: dict[str, Any]) -> ToolResult:
        """Send a message to a remote agent."""
        agent = input.get("agent", "").strip()
        if not agent:
            return ToolResult(
                success=False,
                error={"message": "Agent name or URL required"},
            )

        message = input.get("message", "").strip()
        if not message:
            return ToolResult(
                success=False,
                error={"message": "Message text required"},
            )

        timeout = input.get("timeout")
        blocking = input.get("blocking", True)

        # Resolve to URL
        url = self._resolve_url(agent)
        if not url:
            return ToolResult(
                success=False,
                error={"message": f"Unknown agent: {agent}"},
            )

        # Ensure we have the agent's card (validates reachability)
        if self.registry:
            cached = self.registry.get_cached_card(url)
            if not cached:
                card = await self.client.fetch_agent_card(url)
                self.registry.cache_card(url, card)

        # Send the message
        result = await self.client.send_message(url, message, timeout=timeout)

        if not blocking:
            # Return immediately with task handle
            return ToolResult(success=True, output=result)

        # Blocking: if the task is already terminal, return it
        status = result.get("status", "")
        if status in ("COMPLETED", "FAILED", "REJECTED"):
            return ToolResult(success=True, output=result)

        # If the task is still in progress, poll until terminal or timeout
        import asyncio

        task_id = result.get("id")
        if task_id:
            poll_timeout = timeout or self.config.get("default_timeout", 30.0)
            poll_interval = 1.0
            elapsed = 0.0

            while elapsed < poll_timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    polled = await self.client.get_task_status(url, task_id)
                    polled_status = polled.get("status", "")
                    if polled_status in ("COMPLETED", "FAILED", "REJECTED"):
                        return ToolResult(success=True, output=polled)
                except Exception:
                    break

        # Timeout: return current state
        return ToolResult(success=True, output=result)
```

5. Add the `_op_status` method:

```python
    async def _op_status(self, input: dict[str, Any]) -> ToolResult:
        """Check the status of a remote task."""
        agent = input.get("agent", "").strip()
        if not agent:
            return ToolResult(
                success=False,
                error={"message": "Agent name or URL required"},
            )

        task_id = input.get("task_id", "").strip()
        if not task_id:
            return ToolResult(
                success=False,
                error={"message": "task_id required"},
            )

        url = self._resolve_url(agent)
        if not url:
            return ToolResult(
                success=False,
                error={"message": f"Unknown agent: {agent}"},
            )

        result = await self.client.get_task_status(url, task_id)
        return ToolResult(success=True, output=result)
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/ -v
```

Expected: All tests PASS.

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests PASS.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/
git commit -m "feat(a2a): add async send (blocking=false) and status polling operation"
```

---

## Task 14: LLM Confidence Evaluation

Add `_evaluate_confidence` method to `A2AServer`. Call it after Mode C child session returns for trusted contacts. Uses coordinator's first available provider. On "NO", escalate to Mode A.

**Files:**

- Create: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/evaluation.py`
- Test: `modules/hooks-a2a-server/tests/test_evaluation.py`
- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`

### Step 1: Write the failing tests

Create `modules/hooks-a2a-server/tests/test_evaluation.py`:

```python
"""Tests for LLM confidence evaluation."""

from unittest.mock import AsyncMock, MagicMock


class TestEvaluateConfidence:
    async def test_yes_response_returns_true(self):
        from amplifier_module_hooks_a2a_server.evaluation import evaluate_confidence

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(text="YES")
        )

        result = await evaluate_confidence(
            "What is 2+2?", "4", provider=mock_provider
        )
        assert result is True

    async def test_no_response_returns_false(self):
        from amplifier_module_hooks_a2a_server.evaluation import evaluate_confidence

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(text="NO")
        )

        result = await evaluate_confidence(
            "Tell me a poem", "I don't know any poems", provider=mock_provider
        )
        assert result is False

    async def test_yes_with_extra_text_returns_true(self):
        from amplifier_module_hooks_a2a_server.evaluation import evaluate_confidence

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(text="YES, the response adequately answers.")
        )

        result = await evaluate_confidence(
            "What is 2+2?", "4", provider=mock_provider
        )
        assert result is True

    async def test_timeout_defaults_to_sufficient(self):
        import asyncio
        from amplifier_module_hooks_a2a_server.evaluation import evaluate_confidence

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        result = await evaluate_confidence(
            "What is 2+2?", "4", provider=mock_provider
        )
        assert result is True  # Default to sufficient on error

    async def test_exception_defaults_to_sufficient(self):
        from amplifier_module_hooks_a2a_server.evaluation import evaluate_confidence

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=RuntimeError("Provider crashed")
        )

        result = await evaluate_confidence(
            "What?", "Something", provider=mock_provider
        )
        assert result is True

    async def test_builds_correct_prompt(self):
        from amplifier_module_hooks_a2a_server.evaluation import evaluate_confidence

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(text="YES")
        )

        await evaluate_confidence(
            "What is 2+2?", "The answer is 4.", provider=mock_provider
        )

        call_args = mock_provider.complete.call_args[0][0]
        # Should have system and user messages
        assert len(call_args) == 2
        assert call_args[0]["role"] == "system"
        assert "YES or NO" in call_args[0]["content"]
        assert call_args[1]["role"] == "user"
        assert "What is 2+2?" in call_args[1]["content"]
        assert "The answer is 4." in call_args[1]["content"]
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_evaluation.py -v
```

Expected: FAIL — `ImportError: cannot import name 'evaluate_confidence'`

### Step 3: Write the evaluation module

Create `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/evaluation.py`:

```python
"""LLM confidence evaluation — decides whether Mode C response is sufficient.

Uses a lightweight YES/NO classification prompt. On timeout or error,
defaults to sufficient (don't escalate on evaluation failure — the
child session's response is better than nothing).
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are evaluating whether a response adequately answers a question. "
    "Respond with only YES or NO."
)


async def evaluate_confidence(
    question: str,
    response: str,
    provider: Any,
) -> bool:
    """Evaluate whether a response adequately answers a question.

    Returns True if sufficient (YES), False if insufficient (NO).
    Defaults to True (sufficient) on any error.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Question: {question}\n\nResponse: {response}",
        },
    ]

    try:
        result = await provider.complete(messages)
        text = result.text.strip() if hasattr(result, "text") else str(result).strip()

        first_word = text.split()[0].upper() if text else ""
        is_sufficient = first_word.startswith("YES")

        logger.debug(
            "Confidence evaluation: %s (raw: %s)",
            "sufficient" if is_sufficient else "insufficient",
            text[:50],
        )
        return is_sufficient

    except Exception as e:
        logger.warning(
            "Confidence evaluation failed, defaulting to sufficient: %s", e
        )
        return True
```

### Step 4: Run evaluation tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_evaluation.py -v
```

Expected: All 6 tests PASS.

### Step 5: Wire evaluation into server.py

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`, update the `_handle_mode_c` method and the Mode C path in `handle_send_message`.

First, update `handle_send_message` to pass sender info and tier into `_handle_mode_c`. Find:

```python
        # Mode C: spawn child session to answer (trusted tier, or fallback)
        return await self._handle_mode_c(message, text)
```

Replace with:

```python
        # Mode C: spawn child session to answer (trusted tier, or fallback)
        return await self._handle_mode_c(
            message, text, tier=tier,
            sender_url=sender_url, sender_name=sender_name,
        )
```

Then replace the `_handle_mode_c` method:

```python
    async def _handle_mode_c(
        self,
        message: dict[str, Any],
        text: str,
        tier: str = "trusted",
        sender_url: str = "",
        sender_name: str = "",
    ) -> web.Response:
        """Handle Mode C — spawn child session, optionally evaluate confidence."""
        task_id = self.registry.create_task(message)
        self.registry.update_task(task_id, "WORKING")

        try:
            response_text = await self._spawn_child_session(task_id, text, tier=tier)

            # Confidence evaluation for trusted contacts
            if (
                tier == "trusted"
                and self.config.get("confidence_evaluation", True)
            ):
                is_sufficient = await self._evaluate_response(text, response_text)
                if not is_sufficient:
                    # Escalate to Mode A
                    logger.info(
                        "Confidence evaluation: insufficient for task %s, escalating to Mode A",
                        task_id,
                    )
                    self.registry.update_task(task_id, "INPUT_REQUIRED")
                    pending_queue = self.registry.pending_queue
                    if pending_queue:
                        await pending_queue.add_pending_message(
                            task_id=task_id,
                            sender_url=sender_url,
                            sender_name=sender_name,
                            message=message,
                        )
                    return web.json_response(self.registry.get_task(task_id))

            artifact = {"parts": [{"text": response_text}]}
            self.registry.update_task(task_id, "COMPLETED", artifacts=[artifact])
            return web.json_response(self.registry.get_task(task_id))

        except Exception as e:
            logger.exception("Mode C failed for task %s", task_id)
            self.registry.update_task(task_id, "FAILED", error=str(e))
            return web.json_response(self.registry.get_task(task_id), status=500)

    async def _evaluate_response(self, question: str, response: str) -> bool:
        """Evaluate whether a Mode C response is sufficient.

        Uses the first available provider. Returns True (sufficient) if
        no providers are available or evaluation fails.
        """
        try:
            providers = self.coordinator.mount_points.get("providers", [])
            if not providers:
                logger.debug("No providers available for confidence evaluation")
                return True

            from .evaluation import evaluate_confidence

            provider = providers[0]
            return await evaluate_confidence(question, response, provider=provider)
        except Exception as e:
            logger.warning("Could not access providers for evaluation: %s", e)
            return True
```

### Step 6: Run all tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest -v
```

Expected: All tests PASS.

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/
git commit -m "feat(a2a): add LLM confidence evaluation with Mode C → A escalation"
```

---

## Task 15: Integration Tests — Full Phase 2 Flows

Test the complete flows: first-contact approval, Mode A, Mode C → A escalation, and async send + status polling.

**Files:**

- Create: `tests/test_integration_phase2.py`

### Step 1: Write the integration tests

Create `tests/test_integration_phase2.py`:

```python
"""Integration tests for Phase 2 — full flow tests.

Tests the complete flows:
1. First-contact approval: unknown sender → queued → approve → processed
2. Mode A: known contact → queued → respond → sender gets result
3. Mode C → A escalation: trusted → child session → NO → escalate → respond
4. Async send + status polling

Only AmplifierSession and providers are mocked. All HTTP and data layers are real.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from amplifier_module_hooks_a2a_server.registry import A2ARegistry
from amplifier_module_hooks_a2a_server.card import build_agent_card
from amplifier_module_hooks_a2a_server.server import A2AServer


async def _make_server(tmp_path, known_agents=None, config_overrides=None):
    """Create a full server with real data stores."""
    from amplifier_module_hooks_a2a_server.contacts import ContactStore
    from amplifier_module_hooks_a2a_server.pending import PendingQueue

    config = {
        "port": 0,
        "host": "127.0.0.1",
        "agent_name": "Integration Test Agent",
        **(config_overrides or {}),
    }
    registry = A2ARegistry(known_agents=known_agents)

    contact_store = ContactStore(base_dir=tmp_path)
    pending_queue = PendingQueue(base_dir=tmp_path)
    registry.contact_store = contact_store
    registry.pending_queue = pending_queue

    card = build_agent_card(config)
    coordinator = MagicMock()
    coordinator.session_id = "integration-parent"
    coordinator.parent_id = None
    coordinator.config = {
        "session": {"orchestrator": "loop-basic", "context": "context-simple"},
        "providers": [{"module": "provider-test", "config": {"model": "test"}}],
        "tools": [{"module": "tool-filesystem"}, {"module": "tool-search"}],
        "hooks": [{"module": "hooks-a2a-server"}],
    }
    coordinator.mount_points = {"providers": []}

    server = A2AServer(registry, card, coordinator, config)
    return server, registry, contact_store, pending_queue, coordinator


def _make_mock_session(response_text):
    mock = AsyncMock()
    mock.execute = AsyncMock(return_value=response_text)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


class TestFirstContactApprovalFlow:
    async def test_unknown_sender_approve_then_mode_c(self, tmp_path):
        """Full flow: unknown → INPUT_REQUIRED → approve → message processed."""
        server, registry, contact_store, pending_queue, _ = await _make_server(
            tmp_path
        )
        mock_session = _make_mock_session("The answer is 42")

        await server.start()
        try:
            base_url = f"http://127.0.0.1:{server.port}"

            async with aiohttp.ClientSession() as session:
                # Step 1: Unknown sender sends a message
                async with session.post(
                    f"{base_url}/a2a/v1/message:send",
                    json={
                        "message": {"role": "user", "parts": [{"text": "What is 6*7?"}]},
                        "sender_url": "http://ben:8222",
                        "sender_name": "Ben's Agent",
                    },
                ) as resp:
                    data = await resp.json()
                    assert data["status"] == "INPUT_REQUIRED"
                    task_id = data["id"]

                # Step 2: Verify the approval was queued
                approvals = await pending_queue.list_pending_approvals()
                assert len(approvals) == 1
                assert approvals[0]["sender_url"] == "http://ben:8222"

                # Step 3: Approve the sender
                await contact_store.add_contact(
                    "http://ben:8222", "Ben's Agent", tier="trusted"
                )
                await pending_queue.update_approval_status(
                    "http://ben:8222", "approved"
                )

                # Step 4: Sender sends again — now they're known
                with patch(
                    "amplifier_module_hooks_a2a_server.server.AmplifierSession",
                    return_value=mock_session,
                ):
                    async with session.post(
                        f"{base_url}/a2a/v1/message:send",
                        json={
                            "message": {
                                "role": "user",
                                "parts": [{"text": "What is 6*7?"}],
                            },
                            "sender_url": "http://ben:8222",
                            "sender_name": "Ben's Agent",
                        },
                    ) as resp:
                        data = await resp.json()
                        assert data["status"] == "COMPLETED"
                        assert data["artifacts"][0]["parts"][0]["text"] == "The answer is 42"
        finally:
            await server.stop()


class TestModeAFlow:
    async def test_known_contact_mode_a_respond(self, tmp_path):
        """Full Mode A flow: known → queue → respond → sender polls."""
        server, registry, contact_store, pending_queue, _ = await _make_server(
            tmp_path
        )

        # Pre-add as known contact
        await contact_store.add_contact("http://ben:8222", "Ben", tier="known")

        await server.start()
        try:
            base_url = f"http://127.0.0.1:{server.port}"

            async with aiohttp.ClientSession() as session:
                # Step 1: Known sender sends a message → Mode A
                async with session.post(
                    f"{base_url}/a2a/v1/message:send",
                    json={
                        "message": {
                            "role": "user",
                            "parts": [{"text": "What restaurant tonight?"}],
                        },
                        "sender_url": "http://ben:8222",
                        "sender_name": "Ben",
                    },
                ) as resp:
                    data = await resp.json()
                    assert data["status"] == "INPUT_REQUIRED"
                    task_id = data["id"]

                # Step 2: Verify message is in pending queue
                messages = await pending_queue.list_pending_messages()
                assert len(messages) == 1

                # Step 3: User responds
                artifact = {"parts": [{"text": "Let's go to the Italian place"}]}
                registry.update_task(task_id, "COMPLETED", artifacts=[artifact])
                await pending_queue.update_message_status(task_id, "responded")

                # Step 4: Sender polls and gets the response
                async with session.get(
                    f"{base_url}/a2a/v1/tasks/{task_id}"
                ) as resp:
                    data = await resp.json()
                    assert data["status"] == "COMPLETED"
                    assert (
                        data["artifacts"][0]["parts"][0]["text"]
                        == "Let's go to the Italian place"
                    )
        finally:
            await server.stop()


class TestModeCToAEscalation:
    async def test_low_confidence_escalates_to_mode_a(self, tmp_path):
        """Trusted → Mode C → confidence NO → escalate to Mode A."""
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(text="NO")
        )

        server, registry, contact_store, pending_queue, coordinator = (
            await _make_server(tmp_path, config_overrides={"confidence_evaluation": True})
        )
        coordinator.mount_points = {"providers": [mock_provider]}

        await contact_store.add_contact("http://alice:8222", "Alice", tier="trusted")

        mock_session = _make_mock_session("I'm not sure about that")

        await server.start()
        try:
            base_url = f"http://127.0.0.1:{server.port}"

            with patch(
                "amplifier_module_hooks_a2a_server.server.AmplifierSession",
                return_value=mock_session,
            ):
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{base_url}/a2a/v1/message:send",
                        json={
                            "message": {
                                "role": "user",
                                "parts": [{"text": "What's the secret code?"}],
                            },
                            "sender_url": "http://alice:8222",
                            "sender_name": "Alice",
                        },
                    ) as resp:
                        data = await resp.json()
                        # Should be escalated to INPUT_REQUIRED
                        assert data["status"] == "INPUT_REQUIRED"

                    # Verify it was queued for the user
                    messages = await pending_queue.list_pending_messages()
                    assert len(messages) == 1
                    assert messages[0]["sender_url"] == "http://alice:8222"
        finally:
            await server.stop()


class TestAsyncPolling:
    async def test_poll_task_until_complete(self, tmp_path):
        """Verify task polling works for async sends."""
        server, registry, contact_store, pending_queue, _ = await _make_server(
            tmp_path
        )

        await contact_store.add_contact("http://ben:8222", "Ben", tier="known")

        await server.start()
        try:
            base_url = f"http://127.0.0.1:{server.port}"

            async with aiohttp.ClientSession() as session:
                # Send message (Mode A — returns INPUT_REQUIRED)
                async with session.post(
                    f"{base_url}/a2a/v1/message:send",
                    json={
                        "message": {"role": "user", "parts": [{"text": "Hi"}]},
                        "sender_url": "http://ben:8222",
                        "sender_name": "Ben",
                    },
                ) as resp:
                    data = await resp.json()
                    task_id = data["id"]
                    assert data["status"] == "INPUT_REQUIRED"

                # Poll — still INPUT_REQUIRED
                async with session.get(
                    f"{base_url}/a2a/v1/tasks/{task_id}"
                ) as resp:
                    data = await resp.json()
                    assert data["status"] == "INPUT_REQUIRED"

                # Simulate user response
                artifact = {"parts": [{"text": "Hey there!"}]}
                registry.update_task(task_id, "COMPLETED", artifacts=[artifact])

                # Poll — now COMPLETED
                async with session.get(
                    f"{base_url}/a2a/v1/tasks/{task_id}"
                ) as resp:
                    data = await resp.json()
                    assert data["status"] == "COMPLETED"
                    assert data["artifacts"][0]["parts"][0]["text"] == "Hey there!"
        finally:
            await server.stop()
```

### Step 2: Run the integration tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest tests/test_integration_phase2.py -v
```

Expected: All 4 tests PASS.

### Step 3: Run all tests

```bash
pytest -v
```

Expected: All tests PASS (77 Phase 1 + all Phase 2 additions).

### Step 4: Update LLM instructions

In `context/a2a-instructions.md`, replace the entire contents with:

```markdown
# A2A — Agent-to-Agent Communication

You have access to the `a2a` tool for communicating with remote Amplifier agents on the network.

## Operations

- **`agents`** — List all known remote agents (name, URL, source, tier). Merges configured, discovered, and contact-list agents.
- **`card`** — Fetch a remote agent's identity card. Requires `agent` (name or URL).
- **`send`** — Send a message to a remote agent. Requires `agent` and `message`. Optional: `timeout` (seconds, default 30), `blocking` (default true — set false for async).
- **`status`** — Check the status of a previously sent message. Requires `agent` and `task_id`.
- **`discover`** — Discover agents on the local network via mDNS. Optional `timeout` (seconds, default 2).
- **`contacts`** — List all contacts with their trust tiers.
- **`approve`** — Approve a pending first-contact request. Requires `agent` (URL). Optional `tier` (default "known").
- **`block`** — Block a pending first-contact request. Requires `agent` (URL).
- **`trust`** — Update a contact's trust tier. Requires `agent` (URL) and `tier` ("trusted" or "known").
- **`respond`** — Respond to a pending message from a remote agent. Requires `task_id` and `message`.
- **`dismiss`** — Dismiss a pending message (reject it). Requires `task_id`.

## Usage Pattern

1. Call `a2a(operation="agents")` to see available agents
2. Call `a2a(operation="discover")` to find agents on the local network
3. Call `a2a(operation="send", agent="Agent Name", message="your question")` to communicate
4. For async: use `blocking=false`, then check with `a2a(operation="status", agent="...", task_id="...")`

## Trust & Approval

- When a new (unknown) agent contacts you, you'll see an approval request
- Use `a2a(operation="approve", agent="<url>")` to allow access
- Use `a2a(operation="block", agent="<url>")` to deny access
- Trusted contacts get full tool access; known contacts get limited access

## Pending Messages

- When a known agent sends you a message, you'll see it as a pending message
- Use `a2a(operation="respond", task_id="<id>", message="your reply")` to respond
- Use `a2a(operation="dismiss", task_id="<id>")` to dismiss without responding

## Important

- Messages are sent to remote agents on other devices — they may be controlled by other people
- Blocking sends wait up to 30 seconds by default; use `blocking=false` for immediate return
- If an agent is unreachable, inform the user and suggest trying later
```

### Step 5: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/tests/test_integration_phase2.py
git add amplifier-bundle-a2a/context/a2a-instructions.md
git commit -m "feat(a2a): add Phase 2 integration tests and updated LLM instructions"
```

---

## Summary

Phase 2 adds 15 tasks with the following new files:

| File | Purpose |
|------|---------|
| `contacts.py` | ContactStore — persistent contact management |
| `pending.py` | PendingQueue — pending messages and approval requests |
| `discovery.py` (hooks) | mDNS advertisement |
| `evaluation.py` | LLM confidence evaluation |
| `discovery.py` (tool) | mDNS browsing |
| `test_contacts.py` | 16 tests for ContactStore |
| `test_pending.py` | 16 tests for PendingQueue |
| `test_discovery.py` (hooks) | 5 tests for mDNS advertisement |
| `test_approval.py` | ~11 tests for approval flow + injection |
| `test_mode_a.py` | 4 tests for Mode A server flow |
| `test_evaluation.py` | 6 tests for confidence evaluation |
| `test_discovery.py` (tool) | 3 tests for mDNS browsing |
| `test_integration_phase2.py` | 4 integration tests |

And modified files:

| File | Changes |
|------|---------|
| `registry.py` | `contact_store` and `pending_queue` attributes |
| `server.py` | Contact checking, Mode A routing, capability scoping, confidence evaluation |
| `__init__.py` (hooks) | Create data stores, register hook handler |
| `__init__.py` (tool) | 8 new operations: approve, block, contacts, trust, discover, respond, dismiss, status |
| `client.py` | `sender_url`/`sender_name` in payloads |
| `a2a-instructions.md` | Updated with all Phase 2 operations |
| Both `pyproject.toml` | Added zeroconf dependency |

Total new tests: ~80+. Combined with Phase 1's 77 tests: ~157+ tests total.
