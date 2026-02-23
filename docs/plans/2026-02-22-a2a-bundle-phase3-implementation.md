# A2A Bundle Phase 3 Implementation Plan

> **Execution:** Use the subagent-driven-development workflow to implement this plan.

**Goal:** Add live injection (Mode B), sender-side async response delivery, the full escalation ladder, and attribution metadata to `amplifier-bundle-a2a` — completing the interaction model so incoming requests appear in real time, outgoing responses arrive automatically, and every response carries provenance metadata.

**Architecture:** Phase 3 layers on top of Phase 1 (Mode C) and Phase 2 (Mode A, contacts, trust, discovery, confidence evaluation). The injection handler changes from one-shot to continuous (tracking injected IDs). A background asyncio poller on the sender side detects completed outgoing tasks and injects responses. Attribution metadata is a simple string field added to `_Task` and set at each terminal-state transition. The escalation ladder (C→A→B, B→A via defer) emerges from these components interacting — no separate ladder module.

**Tech Stack:** Python 3.11+, aiohttp (HTTP client/server), amplifier-core (peer dependency), pytest + pytest-asyncio (testing)

**Design Doc:** `docs/plans/2026-02-22-a2a-bundle-phase3-design.md` (at repo root)

---

## Prerequisites

**Working directory:** `/home/bkrabach/dev/a2a-investigate/`

All paths in this plan are relative to `amplifier-bundle-a2a/` unless stated otherwise.

### Dev Environment Setup

The Phase 1 + Phase 2 environment is already set up. No new dependencies for Phase 3.

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
source .venv/bin/activate
```

### Key API Reference (from amplifier-core)

Same as Phase 2, plus these key patterns used in Phase 3:

| API | Signature | Notes |
|-----|-----------|-------|
| HookResult (inject) | `HookResult(action="inject_context", context_injection="...", context_injection_role="user", ephemeral=True, suppress_output=True)` | For injecting messages into active session |
| HookResult (continue) | `HookResult(action="continue")` | Pass-through when nothing to inject |
| Hooks register | `coordinator.hooks.register("provider:request", handler, priority=N, name="...")` | Priority 5 = server injection, 6 = outgoing delivery |
| Register cleanup | `coordinator.register_cleanup(async_fn)` | Called in LIFO order on session end |

### Existing Codebase Reference

**Key files modified in Phase 3:**

| File | Current State |
|------|---------------|
| `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/injection.py` | `A2AInjectionHandler` with `_injected: bool` one-shot flag, `__call__` hook handler |
| `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py` | `PendingQueue` with `add_message()`, `get_pending_messages()`, `get_message()`, `update_message_status()`. Entry fields: `task_id`, `sender_url`, `sender_name`, `message`, `received_at`, `status`. No `escalated` field. |
| `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py` | `A2ARegistry` with `_Task(id, status, history, artifacts, error)`. `update_task(task_id, status, artifacts=None, error=None)`. No `attribution` field. |
| `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py` | `A2AServer.handle_send_message` with inline Mode C logic, confidence evaluation, C→A escalation via `add_message()`. |
| `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py` | `A2ATool` with 11 operations. `mount()` creates tool + registers cleanup. No background tasks. |
| `modules/tool-a2a/amplifier_module_tool_a2a/client.py` | `A2AClient` with `send_message()`, `get_task_status()`, `fetch_agent_card()`. |
| `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py` | `mount()` creates registry, stores, injection handler (priority=5), server, mDNS. |
| `context/a2a-instructions.md` | LLM instructions covering 11 operations. |

### Existing Test Conventions

- Tests use `pytest-asyncio` with `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed on async test methods inside classes)
- Tests use `from unittest.mock import AsyncMock, MagicMock, patch`
- `_make_mock_coordinator()` helper pattern for test setup
- Imports inside test methods (not at module level) for module-specific imports
- `assert result.output is not None` / `assert result.error is not None` type-narrowing guards before subscript access

### Run All Tests Command

After any task, verify no regressions:

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a && source .venv/bin/activate
pytest -v
```

Expected before Phase 3: 206 tests, all passing.

---

## Task 1: Refactor Injection Handler — Tracked IDs Instead of One-Shot Flag

Replace the `_injected: bool` one-shot flag in `A2AInjectionHandler` with `_injected_ids: set[str]` that tracks which task_ids have been injected. The handler now checks for new items on **every** `provider:request` event, injecting only items whose task_id is not in the set. This is the foundation for Mode B (live injection).

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/injection.py`
- Modify: `modules/hooks-a2a-server/tests/test_injection.py`

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_injection.py`:

```python
class TestLiveInjection:
    """Tests for continuous injection (Mode B) — injects new items on every call."""

    async def test_injects_new_message_on_second_call(self):
        """After first injection, a new message arriving should be injected on the next call."""
        from amplifier_module_hooks_a2a_server.injection import A2AInjectionHandler
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue.__new__(PendingQueue)
        queue._messages = [
            {
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "First message"}]},
                "status": "pending",
            }
        ]
        queue._approvals = []

        handler = A2AInjectionHandler(queue)

        # First call — injects task-1
        result1 = await handler("provider:request", {})
        assert result1.action == "inject_context"

        # Add a new message
        queue._messages.append(
            {
                "task_id": "task-2",
                "sender_url": "http://bob:8222",
                "sender_name": "Bob",
                "message": {"parts": [{"text": "Second message"}]},
                "status": "pending",
            }
        )

        # Second call — should inject task-2 (but not task-1 again)
        result2 = await handler("provider:request", {})
        assert result2.action == "inject_context"
        assert "Bob" in result2.context_injection
        assert "Alice" not in result2.context_injection

    async def test_no_injection_when_no_new_items(self):
        """After injecting all items, next call with no new items returns continue."""
        from amplifier_module_hooks_a2a_server.injection import A2AInjectionHandler
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue.__new__(PendingQueue)
        queue._messages = [
            {
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
            }
        ]
        queue._approvals = []

        handler = A2AInjectionHandler(queue)

        # First call — injects
        result1 = await handler("provider:request", {})
        assert result1.action == "inject_context"

        # Second call — nothing new
        result2 = await handler("provider:request", {})
        assert result2.action == "continue"

    async def test_tracks_injected_ids(self):
        """The handler exposes _injected_ids for use by defer operation."""
        from amplifier_module_hooks_a2a_server.injection import A2AInjectionHandler
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue.__new__(PendingQueue)
        queue._messages = [
            {
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
            }
        ]
        queue._approvals = []

        handler = A2AInjectionHandler(queue)
        await handler("provider:request", {})

        assert "task-1" in handler._injected_ids

    async def test_skips_deferred_items(self):
        """Items with status='deferred' are never injected."""
        from amplifier_module_hooks_a2a_server.injection import A2AInjectionHandler
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue.__new__(PendingQueue)
        queue._messages = [
            {
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "deferred",
            }
        ]
        queue._approvals = []

        handler = A2AInjectionHandler(queue)
        result = await handler("provider:request", {})
        assert result.action == "continue"

    async def test_new_approval_injected_on_later_call(self):
        """A new approval arriving after first injection is injected on next call."""
        from amplifier_module_hooks_a2a_server.injection import A2AInjectionHandler
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue.__new__(PendingQueue)
        queue._messages = []
        queue._approvals = [
            {
                "task_id": "approval-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hi"}]},
                "status": "pending",
            }
        ]

        handler = A2AInjectionHandler(queue)

        # First call — injects approval-1
        result1 = await handler("provider:request", {})
        assert result1.action == "inject_context"

        # Add a new approval
        queue._approvals.append(
            {
                "task_id": "approval-2",
                "sender_url": "http://bob:8222",
                "sender_name": "Bob",
                "message": {"parts": [{"text": "Hey"}]},
                "status": "pending",
            }
        )

        # Second call — should inject approval-2
        result2 = await handler("provider:request", {})
        assert result2.action == "inject_context"
        assert "Bob" in result2.context_injection
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_injection.py::TestLiveInjection -v
```

Expected: FAIL — tests fail because the handler still uses a one-shot `_injected` boolean.

### Step 3: Update the injection handler implementation

Replace the **entire contents** of `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/injection.py`:

```python
"""A2A injection handler for provider:request hook.

Checks for pending approval requests and pending messages on EVERY
provider:request event, injecting only items whose task_id has not
been previously injected.  This turns the handler from a "drain on
session start" pattern into a "live stream" pattern (Mode B).
"""

import logging
from typing import Any

from amplifier_core.models import HookResult

from .pending import PendingQueue

logger = logging.getLogger(__name__)


class A2AInjectionHandler:
    """Hook handler that injects pending approvals and messages into the active session.

    Registered on the ``provider:request`` event.  On every call it
    checks for pending approvals and messages whose task_id is NOT in
    ``_injected_ids``.  New items are injected and their IDs added to
    the set.  Items with ``status="deferred"`` are skipped entirely.
    """

    def __init__(self, pending_queue: PendingQueue) -> None:
        self._pending_queue = pending_queue
        self._injected_ids: set[str] = set()

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        """Check for new pending approvals/messages and inject any unseen items."""
        # Gather new approvals (pending status only, not yet injected)
        new_approvals = [
            a
            for a in self._pending_queue.get_pending_approvals()
            if a["task_id"] not in self._injected_ids
        ]

        # Gather new messages (pending status only, not yet injected)
        # Note: get_pending_messages() already filters status=="pending",
        # so deferred items are excluded automatically.
        new_messages = [
            m
            for m in self._pending_queue.get_pending_messages()
            if m["task_id"] not in self._injected_ids
        ]

        if not new_approvals and not new_messages:
            return HookResult(action="continue")

        # Mark these items as injected
        for a in new_approvals:
            self._injected_ids.add(a["task_id"])
        for m in new_messages:
            self._injected_ids.add(m["task_id"])

        # Build injection text
        sections: list[str] = []
        if new_approvals:
            sections.append(self._build_approval_text(new_approvals))
        if new_messages:
            sections.append(self._build_message_text(new_messages))
        text = "\n\n".join(sections)

        return HookResult(
            action="inject_context",
            context_injection=text,
            context_injection_role="user",
            ephemeral=True,
            suppress_output=True,
        )

    @staticmethod
    def _extract_message_text(message: dict) -> str:
        """Extract plain text from message parts."""
        text = ""
        for part in message.get("parts", []):
            if isinstance(part, dict) and "text" in part:
                text += part["text"]
        return text

    @staticmethod
    def _build_approval_text(approvals: list[dict]) -> str:
        """Build the injection string listing all pending approval requests."""
        lines: list[str] = ["<a2a-approval-request>"]
        for approval in approvals:
            sender_name = approval.get("sender_name", "Unknown Agent")
            sender_url = approval.get("sender_url", "unknown")
            lines.append(f'New agent requesting access: "{sender_name}" ({sender_url})')
            msg_text = A2AInjectionHandler._extract_message_text(
                approval.get("message", {})
            )
            if msg_text:
                lines.append(f'Message: "{msg_text}"')
            lines.append(
                f'Use a2a(operation="approve", agent="{sender_url}") to allow,'
            )
            lines.append(f'or a2a(operation="block", agent="{sender_url}") to block.')
        lines.append("</a2a-approval-request>")
        return "\n".join(lines)

    @staticmethod
    def _build_message_text(messages: list[dict]) -> str:
        """Build the injection string listing pending messages."""
        count = len(messages)
        noun = "message" if count == 1 else "messages"
        lines: list[str] = [
            "<a2a-pending-messages>",
            f"You have {count} pending {noun} from a remote agent:",
        ]
        for msg_entry in messages:
            sender_name = msg_entry.get("sender_name", "Unknown Agent")
            sender_url = msg_entry.get("sender_url", "unknown")
            task_id = msg_entry.get("task_id", "unknown")
            lines.append("")
            lines.append(f"From: {sender_name} ({sender_url})")
            msg_text = A2AInjectionHandler._extract_message_text(
                msg_entry.get("message", {})
            )
            if msg_text:
                lines.append(f'Message: "{msg_text}"')
            lines.append(
                f'Use a2a(operation="respond", task_id="{task_id}", message="your reply") to respond,'
            )
            lines.append(
                f'or a2a(operation="defer", task_id="{task_id}") to handle later,'
            )
            lines.append(
                f'or a2a(operation="dismiss", task_id="{task_id}") to dismiss.'
            )
        lines.append("</a2a-pending-messages>")
        return "\n".join(lines)
```

### Step 4: Update existing injection tests

Some existing tests in `test_injection.py` relied on the one-shot behavior. The test class `TestInjectionOnlyOnce` needs to be updated to reflect the new tracked-IDs behavior. Find the class `TestInjectionOnlyOnce` and update its test:

In `modules/hooks-a2a-server/tests/test_injection.py`, find the test that asserts the second call returns `continue` after the first injection. This test is **still correct** because when no new items arrive between calls, the handler returns `continue`. The tracked-IDs approach still returns `continue` for already-injected items. No changes needed to existing tests if they follow this pattern.

**However**, if any test asserts that `handler._injected` exists (the old boolean attribute), update it to check `handler._injected_ids` instead:
- Replace `assert handler._injected is True` → `assert len(handler._injected_ids) > 0`
- Replace `handler._injected` → `handler._injected_ids`

Also, `TestInjectionFlagCoversBothTypes` (if it checks `_injected` directly) should be updated similarly.

### Step 5: Run all injection tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_injection.py -v
```

Expected: All injection tests PASS (existing + 5 new).

### Step 6: Run all tests to check for regressions

```bash
pytest -v
```

Expected: All 211+ tests pass (206 + 5 new).

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/injection.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_injection.py
git commit -m "feat(a2a): refactor injection handler from one-shot flag to tracked IDs (Mode B)"
```

---

## Task 2: Add `defer` Tool Operation

Add `a2a(operation="defer", task_id="...")` to `tool-a2a`. Marks the pending item as "deferred" in PendingQueue and adds the task_id to the injection handler's `_injected_ids` set (via the registry). The item stays in the queue for manual `respond` later.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/tests/test_tool_a2a.py`
- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py` (add `injection_handler` attribute)
- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py` (wire injection handler into registry)

### Step 1: Write the failing tests

Add to the **end** of `modules/tool-a2a/tests/test_tool_a2a.py`:

```python
class TestDeferOperation:
    async def test_defer_marks_message_deferred(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.contact_store = MagicMock()
        # Mock injection handler with _injected_ids
        registry.injection_handler = MagicMock()
        registry.injection_handler._injected_ids = set()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "defer", "task_id": "task-1"})
        assert result.success is True
        registry.pending_queue.update_message_status.assert_called_once_with(
            "task-1", "deferred"
        )

    async def test_defer_adds_to_injected_ids(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.contact_store = MagicMock()
        registry.injection_handler = MagicMock()
        registry.injection_handler._injected_ids = set()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        await tool.execute({"operation": "defer", "task_id": "task-1"})
        assert "task-1" in registry.injection_handler._injected_ids

    async def test_defer_requires_task_id(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "defer"})
        assert result.success is False
        assert result.error is not None
        assert "task_id" in result.error["message"].lower()

    async def test_defer_unknown_task_returns_error(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(return_value=None)
        registry.contact_store = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute(
            {"operation": "defer", "task_id": "nonexistent"}
        )
        assert result.success is False

    async def test_defer_already_responded_returns_error(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "responded",
            }
        )
        registry.contact_store = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool.execute({"operation": "defer", "task_id": "task-1"})
        assert result.success is False
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestDeferOperation -v
```

Expected: FAIL — `Unknown operation: defer`

### Step 3: Add `injection_handler` attribute to registry

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py`, find:

```python
        self.contact_store: Any | None = None  # Set by mount()
        self.pending_queue: Any | None = None  # Set by mount()
```

Replace with:

```python
        self.contact_store: Any | None = None  # Set by mount()
        self.pending_queue: Any | None = None  # Set by mount()
        self.injection_handler: Any | None = None  # Set by mount()
```

### Step 4: Wire injection handler into registry in mount()

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py`, find:

```python
    injection_handler = A2AInjectionHandler(registry.pending_queue)
    coordinator.hooks.register(
        "provider:request",
        injection_handler,
        priority=5,
        name="a2a-pending-injection",
    )
```

Replace with:

```python
    injection_handler = A2AInjectionHandler(registry.pending_queue)
    registry.injection_handler = injection_handler
    coordinator.hooks.register(
        "provider:request",
        injection_handler,
        priority=5,
        name="a2a-pending-injection",
    )
```

### Step 5: Add `defer` operation to A2ATool

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`:

**5a.** Update the `input_schema` property. Find the enum list:

```python
                    "enum": [
                        "agents",
                        "card",
                        "send",
                        "status",
                        "discover",
                        "approve",
                        "block",
                        "contacts",
                        "trust",
                        "respond",
                        "dismiss",
                    ],
```

Replace with:

```python
                    "enum": [
                        "agents",
                        "card",
                        "send",
                        "status",
                        "discover",
                        "approve",
                        "block",
                        "contacts",
                        "trust",
                        "respond",
                        "dismiss",
                        "defer",
                    ],
```

**5b.** Update the `description` property. Find:

```python
            "'respond' (reply to a pending message), "
            "'dismiss' (dismiss a pending message)."
```

Replace with:

```python
            "'respond' (reply to a pending message), "
            "'dismiss' (dismiss a pending message), "
            "'defer' (handle a pending message later)."
```

**5c.** Add dispatch in `execute`. Find:

```python
            elif operation == "dismiss":
                return await self._op_dismiss(input)
```

Add after it:

```python
            elif operation == "defer":
                return await self._op_defer(input)
```

**5d.** Add the `_op_defer` method. Add before the `_resolve_url` method:

```python
    async def _op_defer(self, input: dict[str, Any]) -> ToolResult:
        """Defer a pending message to handle later (Mode B → A downgrade)."""
        err = self._check_stores()
        if err:
            return err

        task_id = input.get("task_id", "").strip()
        if not task_id:
            return ToolResult(
                success=False,
                error={"message": "task_id required"},
            )

        pending = self.registry.pending_queue.get_message(task_id)
        if not pending:
            return ToolResult(
                success=False,
                error={"message": f"No pending message with task_id: {task_id}"},
            )

        if pending["status"] not in ("pending", "deferred"):
            return ToolResult(
                success=False,
                error={
                    "message": (
                        f"This message has already been {pending['status']}. "
                        "Cannot defer."
                    )
                },
            )

        # Mark as deferred in the queue
        await self.registry.pending_queue.update_message_status(task_id, "deferred")

        # Add to injection handler's injected set so it won't be re-injected
        injection_handler = getattr(self.registry, "injection_handler", None)
        if injection_handler and hasattr(injection_handler, "_injected_ids"):
            injection_handler._injected_ids.add(task_id)

        return ToolResult(
            success=True,
            output=(
                f"Message {task_id} deferred. It will not appear again automatically. "
                f'Use a2a(operation="respond", task_id="{task_id}", message="...") '
                "to respond later."
            ),
        )
```

**5e.** Update the module docstring at the top of the file. Find:

```python
  - dismiss: dismiss a pending Mode A message
```

Add after it:

```python
  - defer: defer a pending message to handle later
```

### Step 6: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py -v
```

Expected: All tool tests PASS (existing + 5 new).

### Step 7: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 8: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/__init__.py
git add amplifier-bundle-a2a/modules/tool-a2a/amplifier_module_tool_a2a/__init__.py
git add amplifier-bundle-a2a/modules/tool-a2a/tests/test_tool_a2a.py
git commit -m "feat(a2a): add defer operation for Mode B → A downgrade"
```

---

## Task 3: PendingQueue — `escalated` Flag on Message Entries

Add an `escalated: bool` field to pending message entries. Set to `True` when Mode C escalates to Mode A (confidence evaluator said NO). Default `False` for messages that go directly to Mode A. The `_op_respond` method will use this flag to determine the correct attribution value (Task 6).

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py`
- Modify: `modules/hooks-a2a-server/tests/test_pending.py`

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_pending.py`:

```python
class TestPendingQueueEscalatedFlag:
    """Tests for the escalated flag on pending message entries."""

    async def test_add_message_default_not_escalated(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_message(
            task_id="task-1",
            sender_url="http://alice:8222",
            sender_name="Alice",
            message={"parts": [{"text": "Hello"}]},
        )

        entry = queue.get_message("task-1")
        assert entry is not None
        assert entry["escalated"] is False

    async def test_add_message_escalated_true(self, tmp_path):
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_message(
            task_id="task-1",
            sender_url="http://alice:8222",
            sender_name="Alice",
            message={"parts": [{"text": "Hello"}]},
            escalated=True,
        )

        entry = queue.get_message("task-1")
        assert entry is not None
        assert entry["escalated"] is True

    async def test_escalated_persists_to_disk(self, tmp_path):
        import json
        from amplifier_module_hooks_a2a_server.pending import PendingQueue

        queue = PendingQueue(base_dir=tmp_path)
        await queue.add_message(
            task_id="task-1",
            sender_url="http://alice:8222",
            sender_name="Alice",
            message={"parts": [{"text": "Hello"}]},
            escalated=True,
        )

        messages_file = tmp_path / "pending_messages.json"
        data = json.loads(messages_file.read_text())
        assert data[0]["escalated"] is True
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_pending.py::TestPendingQueueEscalatedFlag -v
```

Expected: FAIL — `add_message() got an unexpected keyword argument 'escalated'`

### Step 3: Update `add_message` in PendingQueue

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py`, find:

```python
    async def add_message(
        self,
        task_id: str,
        sender_url: str,
        sender_name: str,
        message: dict,
    ) -> None:
        """Add an entry to pending_messages.json."""
        async with self._lock:
            self._messages.append(
                {
                    "task_id": task_id,
                    "sender_url": sender_url,
                    "sender_name": sender_name,
                    "message": message,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                }
            )
            self._save(self._messages_path, self._messages)
```

Replace with:

```python
    async def add_message(
        self,
        task_id: str,
        sender_url: str,
        sender_name: str,
        message: dict,
        escalated: bool = False,
    ) -> None:
        """Add an entry to pending_messages.json."""
        async with self._lock:
            self._messages.append(
                {
                    "task_id": task_id,
                    "sender_url": sender_url,
                    "sender_name": sender_name,
                    "message": message,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                    "escalated": escalated,
                }
            )
            self._save(self._messages_path, self._messages)
```

### Step 4: Run all pending tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_pending.py -v
```

Expected: All pending tests PASS (existing + 3 new).

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/pending.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_pending.py
git commit -m "feat(a2a): add escalated flag to PendingQueue message entries"
```

---

## Task 4: Attribution Field on Registry Tasks

Extend `_Task` dataclass with an `attribution: str | None` field. Update `update_task` to accept optional `attribution` parameter. Include it in `get_task` output.

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py`
- Modify: `modules/hooks-a2a-server/tests/test_registry.py`

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_registry.py`:

```python
class TestAttribution:
    """Tests for attribution metadata on tasks."""

    def test_task_default_attribution_is_none(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        task_id = registry.create_task({"parts": [{"text": "Hello"}]})
        task = registry.get_task(task_id)
        assert task is not None
        assert "attribution" not in task  # None attribution not included

    def test_update_task_with_attribution(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        task_id = registry.create_task({"parts": [{"text": "Hello"}]})
        registry.update_task(task_id, "COMPLETED", attribution="autonomous")
        task = registry.get_task(task_id)
        assert task is not None
        assert task["attribution"] == "autonomous"

    def test_attribution_values(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        for value in [
            "autonomous",
            "user_response",
            "escalated_user_response",
            "dismissed",
        ]:
            task_id = registry.create_task({"parts": [{"text": "Hello"}]})
            registry.update_task(task_id, "COMPLETED", attribution=value)
            task = registry.get_task(task_id)
            assert task is not None
            assert task["attribution"] == value

    def test_update_task_without_attribution_preserves_existing(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        task_id = registry.create_task({"parts": [{"text": "Hello"}]})
        registry.update_task(task_id, "WORKING", attribution="autonomous")
        # Update status only — attribution should be preserved
        registry.update_task(task_id, "COMPLETED")
        task = registry.get_task(task_id)
        assert task is not None
        assert task["attribution"] == "autonomous"

    def test_get_task_includes_attribution_when_set(self):
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry

        registry = A2ARegistry()
        task_id = registry.create_task({"parts": [{"text": "Hello"}]})
        registry.update_task(
            task_id,
            "COMPLETED",
            artifacts=[{"parts": [{"text": "Answer"}]}],
            attribution="user_response",
        )
        task = registry.get_task(task_id)
        assert task is not None
        assert task["attribution"] == "user_response"
        assert task["status"] == "COMPLETED"
        assert len(task["artifacts"]) == 1
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_registry.py::TestAttribution -v
```

Expected: FAIL — `update_task() got an unexpected keyword argument 'attribution'`

### Step 3: Update `_Task` and `update_task` and `get_task`

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py`:

**3a.** Update the `_Task` dataclass. Find:

```python
@dataclass
class _Task:
    id: str
    status: str  # SUBMITTED, WORKING, COMPLETED, FAILED, INPUT_REQUIRED
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
```

Replace with:

```python
@dataclass
class _Task:
    id: str
    status: str  # SUBMITTED, WORKING, COMPLETED, FAILED, INPUT_REQUIRED
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    attribution: str | None = None
```

**3b.** Update `update_task`. Find:

```python
    def update_task(
        self,
        task_id: str,
        status: str,
        artifacts: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        """Update a task's status, artifacts, and/or error."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = status
        if artifacts is not None:
            task.artifacts = artifacts
        if error is not None:
            task.error = error
```

Replace with:

```python
    def update_task(
        self,
        task_id: str,
        status: str,
        artifacts: list[dict[str, Any]] | None = None,
        error: str | None = None,
        attribution: str | None = None,
    ) -> None:
        """Update a task's status, artifacts, error, and/or attribution."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = status
        if artifacts is not None:
            task.artifacts = artifacts
        if error is not None:
            task.error = error
        if attribution is not None:
            task.attribution = attribution
```

**3c.** Update `get_task`. Find:

```python
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a task by ID, or None if not found."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        result: dict[str, Any] = {
            "id": task.id,
            "status": task.status,
            "history": task.history,
            "artifacts": task.artifacts,
        }
        if task.error is not None:
            result["error"] = task.error
        return result
```

Replace with:

```python
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a task by ID, or None if not found."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        result: dict[str, Any] = {
            "id": task.id,
            "status": task.status,
            "history": task.history,
            "artifacts": task.artifacts,
        }
        if task.error is not None:
            result["error"] = task.error
        if task.attribution is not None:
            result["attribution"] = task.attribution
        return result
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_registry.py -v
```

Expected: All registry tests PASS (existing + 5 new).

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/registry.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_registry.py
git commit -m "feat(a2a): add attribution field to registry tasks"
```

---

## Task 5: Set Attribution on Mode C Success and C→A Escalation

Modify `handle_send_message` in `server.py`:
- When Mode C completes successfully: set `attribution: "autonomous"`
- When Mode C escalates to Mode A: pass `escalated=True` to `add_message()`

**Files:**

- Modify: `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`
- Modify: `modules/hooks-a2a-server/tests/test_mode_c.py`
- Modify: `modules/hooks-a2a-server/tests/test_evaluation.py`

### Step 1: Write the failing tests

Add to the **end** of `modules/hooks-a2a-server/tests/test_mode_c.py`:

```python
class TestModeCAttribution:
    """Tests for attribution set on Mode C success."""

    async def test_mode_c_success_sets_autonomous_attribution(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from aiohttp.test_utils import (
            TestClient as AioTestClient,
            TestServer as AioTestServer,
        )

        from amplifier_module_hooks_a2a_server.card import build_agent_card
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry
        from amplifier_module_hooks_a2a_server.server import A2AServer

        config = {"port": 0, "agent_name": "Test", "confidence_evaluation": False}
        registry = A2ARegistry()
        card = build_agent_card(config)
        coordinator = MagicMock()
        coordinator.session_id = "test-session"
        coordinator.parent_id = None
        coordinator.config = {
            "session": {"orchestrator": "loop-basic"},
            "providers": [{"module": "provider-test"}],
            "tools": [],
        }

        server = A2AServer(registry, card, coordinator, config)

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
                        "message": {"role": "user", "parts": [{"text": "Hello"}]},
                        "sender_url": "http://sender:8222",
                    },
                )
                data = await resp.json()
                assert data["status"] == "COMPLETED"
                assert data["attribution"] == "autonomous"
```

Add to the **end** of `modules/hooks-a2a-server/tests/test_evaluation.py`:

```python
class TestEscalationSetsEscalatedFlag:
    """Tests for C→A escalation setting the escalated flag."""

    async def test_escalation_sets_escalated_true_on_pending_message(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from aiohttp.test_utils import (
            TestClient as AioTestClient,
            TestServer as AioTestServer,
        )

        from amplifier_module_hooks_a2a_server.card import build_agent_card
        from amplifier_module_hooks_a2a_server.contacts import ContactStore
        from amplifier_module_hooks_a2a_server.pending import PendingQueue
        from amplifier_module_hooks_a2a_server.registry import A2ARegistry
        from amplifier_module_hooks_a2a_server.server import A2AServer

        config = {"port": 0, "agent_name": "Test", "confidence_evaluation": True}
        registry = A2ARegistry()
        card = build_agent_card(config)

        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        contact_store = ContactStore(base_dir=tmp)
        await contact_store.add_contact(
            "http://sender:8222", "Sender", "trusted"
        )
        pending_queue = PendingQueue(base_dir=tmp)
        registry.contact_store = contact_store
        registry.pending_queue = pending_queue

        # Mock coordinator with a provider for confidence evaluation
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value={"content": "NO"}
        )
        coordinator = MagicMock()
        coordinator.session_id = "test-session"
        coordinator.parent_id = None
        coordinator.config = {
            "session": {"orchestrator": "loop-basic"},
            "providers": [{"module": "provider-test"}],
            "tools": [],
        }
        coordinator.mount_points = {"providers": [mock_provider]}

        server = A2AServer(registry, card, coordinator, config)

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value="I'm not sure")
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
                        "message": {"role": "user", "parts": [{"text": "Hello"}]},
                        "sender_url": "http://sender:8222",
                    },
                )
                data = await resp.json()
                assert data["status"] == "INPUT_REQUIRED"

        # Verify the pending message has escalated=True
        messages = pending_queue.get_pending_messages()
        assert len(messages) == 1
        assert messages[0].get("escalated") is True
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_mode_c.py::TestModeCAttribution -v
pytest modules/hooks-a2a-server/tests/test_evaluation.py::TestEscalationSetsEscalatedFlag -v
```

Expected: FAIL — `"attribution"` not in response data, `"escalated"` not in pending entry.

### Step 3: Update server.py

In `modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py`:

**3a.** Set `attribution="autonomous"` on Mode C success. Find (around line 196-197):

```python
            artifact = {"parts": [{"text": response_text}]}
            self.registry.update_task(task_id, "COMPLETED", artifacts=[artifact])
```

Replace with:

```python
            artifact = {"parts": [{"text": response_text}]}
            self.registry.update_task(
                task_id, "COMPLETED", artifacts=[artifact], attribution="autonomous"
            )
```

**3b.** Pass `escalated=True` on C→A escalation. Find (around line 185-193):

```python
                        self.registry.update_task(task_id, "INPUT_REQUIRED")
                        pending_queue = getattr(self.registry, "pending_queue", None)
                        if pending_queue is not None:
                            await pending_queue.add_message(
                                task_id=task_id,
                                sender_url=sender_url,
                                sender_name=sender_name,
                                message=message,
                            )
```

Replace with:

```python
                        self.registry.update_task(task_id, "INPUT_REQUIRED")
                        pending_queue = getattr(self.registry, "pending_queue", None)
                        if pending_queue is not None:
                            await pending_queue.add_message(
                                task_id=task_id,
                                sender_url=sender_url,
                                sender_name=sender_name,
                                message=message,
                                escalated=True,
                            )
```

### Step 4: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/hooks-a2a-server/tests/test_mode_c.py -v
pytest modules/hooks-a2a-server/tests/test_evaluation.py -v
```

Expected: All tests PASS.

### Step 5: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 6: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/hooks-a2a-server/amplifier_module_hooks_a2a_server/server.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_mode_c.py
git add amplifier-bundle-a2a/modules/hooks-a2a-server/tests/test_evaluation.py
git commit -m "feat(a2a): set attribution on Mode C success and escalated flag on C→A"
```

---

## Task 6: Set Attribution on Respond, Dismiss, and Escalated Responses

Modify `_op_respond` in tool to check the pending entry's `escalated` flag and set the correct attribution value (`"escalated_user_response"` or `"user_response"`). Modify `_op_dismiss` to set `attribution: "dismissed"`.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/tests/test_tool_a2a.py`

### Step 1: Write the failing tests

Add to the **end** of `modules/tool-a2a/tests/test_tool_a2a.py`:

```python
class TestRespondAttribution:
    """Tests for attribution set when responding to messages."""

    async def test_respond_sets_user_response_attribution(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
                "escalated": False,
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.contact_store = MagicMock()
        registry.update_task = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        await tool.execute(
            {"operation": "respond", "task_id": "task-1", "message": "My reply"}
        )

        registry.update_task.assert_called_once()
        call_kwargs = registry.update_task.call_args
        assert call_kwargs[1].get("attribution") == "user_response" or (
            len(call_kwargs[0]) > 3 and call_kwargs[0][3] is not None
        )

    async def test_respond_sets_escalated_user_response_attribution(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
                "escalated": True,
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.contact_store = MagicMock()
        registry.update_task = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        await tool.execute(
            {"operation": "respond", "task_id": "task-1", "message": "My reply"}
        )

        registry.update_task.assert_called_once()
        call_args = registry.update_task.call_args
        # Check attribution is "escalated_user_response"
        assert call_args[1].get("attribution") == "escalated_user_response"


class TestDismissAttribution:
    """Tests for attribution set when dismissing messages."""

    async def test_dismiss_sets_dismissed_attribution(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        registry.pending_queue = MagicMock()
        registry.pending_queue.get_message = MagicMock(
            return_value={
                "task_id": "task-1",
                "sender_url": "http://alice:8222",
                "sender_name": "Alice",
                "message": {"parts": [{"text": "Hello"}]},
                "status": "pending",
            }
        )
        registry.pending_queue.update_message_status = AsyncMock()
        registry.contact_store = MagicMock()
        registry.update_task = MagicMock()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        await tool.execute({"operation": "dismiss", "task_id": "task-1"})

        registry.update_task.assert_called_once()
        call_args = registry.update_task.call_args
        assert call_args[1].get("attribution") == "dismissed"
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestRespondAttribution -v
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestDismissAttribution -v
```

Expected: FAIL — `update_task` not called with `attribution` keyword.

### Step 3: Update `_op_respond` in A2ATool

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, find the `_op_respond` method. Replace:

```python
        self.registry.update_task(
            task_id,
            "COMPLETED",
            artifacts=[{"parts": [{"text": message}]}],
        )
```

With:

```python
        # Determine attribution based on escalation status
        escalated = pending.get("escalated", False)
        attribution = "escalated_user_response" if escalated else "user_response"

        self.registry.update_task(
            task_id,
            "COMPLETED",
            artifacts=[{"parts": [{"text": message}]}],
            attribution=attribution,
        )
```

### Step 4: Update `_op_dismiss` in A2ATool

Find the `_op_dismiss` method. Replace:

```python
        self.registry.update_task(
            task_id,
            "REJECTED",
            error="Dismissed by user",
        )
```

With:

```python
        self.registry.update_task(
            task_id,
            "REJECTED",
            error="Dismissed by user",
            attribution="dismissed",
        )
```

### Step 5: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py -v
```

Expected: All tool tests PASS.

### Step 6: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/amplifier_module_tool_a2a/__init__.py
git add amplifier-bundle-a2a/modules/tool-a2a/tests/test_tool_a2a.py
git commit -m "feat(a2a): set attribution on respond (user/escalated) and dismiss"
```

---

## Task 7: Sender-Side Outgoing Task Tracking

Add `_pending_outgoing: dict[str, dict]` and `_completed_outgoing: list[dict]` to `A2ATool`. When a non-blocking send returns, or a blocking send times out, store the outgoing task info. This data structure is the foundation for the background poller (Task 8) and response injection (Task 9).

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Modify: `modules/tool-a2a/tests/test_tool_a2a.py`

### Step 1: Write the failing tests

Add to the **end** of `modules/tool-a2a/tests/test_tool_a2a.py`:

```python
class TestOutgoingTracking:
    """Tests for sender-side outgoing task tracking."""

    async def test_nonblocking_send_tracks_outgoing(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})
        tool.client = AsyncMock()
        tool.client.send_message = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "SUBMITTED",
            }
        )
        tool.client.fetch_agent_card = AsyncMock(return_value={"name": "Test"})

        await tool.execute(
            {
                "operation": "send",
                "agent": "http://remote:8222",
                "message": "Hello",
                "blocking": False,
            }
        )

        assert "task-1" in tool._pending_outgoing
        entry = tool._pending_outgoing["task-1"]
        assert entry["agent_url"] == "http://remote:8222"
        assert "submitted_at" in entry

    async def test_blocking_send_completed_not_tracked(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})
        tool.client = AsyncMock()
        tool.client.send_message = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "COMPLETED",
                "artifacts": [{"parts": [{"text": "Done"}]}],
            }
        )
        tool.client.fetch_agent_card = AsyncMock(return_value={"name": "Test"})

        await tool.execute(
            {
                "operation": "send",
                "agent": "http://remote:8222",
                "message": "Hello",
                "blocking": True,
            }
        )

        # Completed immediately — should NOT be tracked
        assert "task-1" not in tool._pending_outgoing

    async def test_blocking_send_timeout_tracks_outgoing(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})
        tool.client = AsyncMock()
        tool.client.send_message = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "WORKING",
            }
        )
        tool.client.fetch_agent_card = AsyncMock(return_value={"name": "Test"})
        tool.client.get_task_status = AsyncMock(
            return_value={"id": "task-1", "status": "WORKING"}
        )

        await tool.execute(
            {
                "operation": "send",
                "agent": "http://remote:8222",
                "message": "Hello",
                "blocking": True,
                "timeout": 0.1,
            }
        )

        # Timed out — should be tracked for async delivery
        assert "task-1" in tool._pending_outgoing

    async def test_completed_outgoing_starts_empty(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        assert tool._completed_outgoing == []
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py::TestOutgoingTracking -v
```

Expected: FAIL — `A2ATool` has no `_pending_outgoing` attribute.

### Step 3: Add tracking attributes to A2ATool

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, find:

```python
    def __init__(self, coordinator: Any, config: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self.config = config
        self.client = A2AClient(timeout=config.get("default_timeout", 30.0))
        self._registry: Any = None  # Lazy — hook may not have mounted yet
```

Replace with:

```python
    def __init__(self, coordinator: Any, config: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self.config = config
        self.client = A2AClient(timeout=config.get("default_timeout", 30.0))
        self._registry: Any = None  # Lazy — hook may not have mounted yet
        self._pending_outgoing: dict[str, dict[str, Any]] = {}
        self._completed_outgoing: list[dict[str, Any]] = []
```

### Step 4: Track outgoing tasks in `_op_send`

In the `_op_send` method, find the non-blocking return:

```python
        if not blocking:
            # Return immediately with whatever status we got
            return ToolResult(success=True, output=result)
```

Replace with:

```python
        if not blocking:
            # Track for async delivery
            task_id = result.get("id")
            if task_id and result.get("status") not in {"COMPLETED", "FAILED", "REJECTED"}:
                self._pending_outgoing[task_id] = {
                    "task_id": task_id,
                    "agent_url": url,
                    "agent_name": agent,
                    "submitted_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
                }
            return ToolResult(success=True, output=result)
```

Then find the timeout return at the end of `_op_send`:

```python
        # Timeout: return last known state with the task handle
        return ToolResult(
            success=True,
            output={
                "id": task_id,
                "status": result.get("status", "UNKNOWN"),
                "message": (
                    f"Timeout after {timeout}s. Use "
                    f"a2a(operation='status', agent='{agent}', "
                    f"task_id='{task_id}') to check later."
                ),
            },
        )
```

Replace with:

```python
        # Timeout: track for async delivery and return task handle
        if task_id:
            self._pending_outgoing[task_id] = {
                "task_id": task_id,
                "agent_url": url,
                "agent_name": agent,
                "submitted_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
            }
        return ToolResult(
            success=True,
            output={
                "id": task_id,
                "status": result.get("status", "UNKNOWN"),
                "message": (
                    f"Timeout after {timeout}s. Response will be delivered "
                    f"automatically when available."
                ),
            },
        )
```

Also add `from datetime import datetime, timezone` to the top-level imports. Find:

```python
import asyncio
import logging
from typing import Any
```

Replace with:

```python
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
```

Then replace the `__import__("datetime")` patterns with proper imports. In the non-blocking tracking:

```python
                    "submitted_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
```

Replace with:

```python
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
```

And the same for the timeout tracking block.

### Step 5: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_tool_a2a.py -v
```

Expected: All tool tests PASS.

### Step 6: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/amplifier_module_tool_a2a/__init__.py
git add amplifier-bundle-a2a/modules/tool-a2a/tests/test_tool_a2a.py
git commit -m "feat(a2a): add sender-side outgoing task tracking on A2ATool"
```

---

## Task 8: Background Outgoing Poller

Add a background asyncio task (`_outgoing_poller_task`) to `A2ATool`. Started during `mount()`, polls every 5 seconds (configurable via `config.poll_interval`), checks each `_pending_outgoing` task via `client.get_task_status()`. Terminal states move to `_completed_outgoing`. Cancelled during cleanup.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Create: `modules/tool-a2a/tests/test_outgoing_poller.py`

### Step 1: Write the failing tests

Create `modules/tool-a2a/tests/test_outgoing_poller.py`:

```python
"""Tests for the background outgoing task poller."""

import asyncio

from unittest.mock import AsyncMock, MagicMock


def _make_mock_registry():
    registry = MagicMock()
    registry.resolve_agent_url = MagicMock(side_effect=lambda x: x)
    registry.get_cached_card = MagicMock(return_value={"name": "Test"})
    registry.contact_store = MagicMock()
    registry.pending_queue = MagicMock()
    return registry


def _make_mock_coordinator(registry=None):
    coordinator = MagicMock()
    coordinator.get_capability = MagicMock(return_value=registry)
    coordinator.hooks = MagicMock()
    coordinator.register_cleanup = MagicMock()
    return coordinator


class TestOutgoingPoller:
    async def test_poller_detects_completed_task(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {"poll_interval": 0.1})
        tool.client = AsyncMock()
        tool.client.get_task_status = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "COMPLETED",
                "artifacts": [{"parts": [{"text": "Answer"}]}],
                "attribution": "autonomous",
            }
        )

        # Add a pending outgoing task
        tool._pending_outgoing["task-1"] = {
            "task_id": "task-1",
            "agent_url": "http://remote:8222",
            "agent_name": "Remote",
            "submitted_at": "2026-01-01T00:00:00Z",
        }

        # Run one poll cycle
        await tool._poll_outgoing_once()

        assert "task-1" not in tool._pending_outgoing
        assert len(tool._completed_outgoing) == 1
        assert tool._completed_outgoing[0]["task_id"] == "task-1"
        assert tool._completed_outgoing[0]["status"] == "COMPLETED"

    async def test_poller_leaves_working_tasks(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {"poll_interval": 0.1})
        tool.client = AsyncMock()
        tool.client.get_task_status = AsyncMock(
            return_value={"id": "task-1", "status": "WORKING"}
        )

        tool._pending_outgoing["task-1"] = {
            "task_id": "task-1",
            "agent_url": "http://remote:8222",
            "agent_name": "Remote",
            "submitted_at": "2026-01-01T00:00:00Z",
        }

        await tool._poll_outgoing_once()

        # Still pending
        assert "task-1" in tool._pending_outgoing
        assert len(tool._completed_outgoing) == 0

    async def test_poller_handles_connection_error(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {"poll_interval": 0.1})
        tool.client = AsyncMock()
        tool.client.get_task_status = AsyncMock(
            side_effect=ConnectionError("unreachable")
        )

        tool._pending_outgoing["task-1"] = {
            "task_id": "task-1",
            "agent_url": "http://remote:8222",
            "agent_name": "Remote",
            "submitted_at": "2026-01-01T00:00:00Z",
        }

        # Should not crash — just skip this cycle
        await tool._poll_outgoing_once()

        assert "task-1" in tool._pending_outgoing

    async def test_poller_detects_failed_task(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {"poll_interval": 0.1})
        tool.client = AsyncMock()
        tool.client.get_task_status = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "FAILED",
                "error": "Something went wrong",
            }
        )

        tool._pending_outgoing["task-1"] = {
            "task_id": "task-1",
            "agent_url": "http://remote:8222",
            "agent_name": "Remote",
            "submitted_at": "2026-01-01T00:00:00Z",
        }

        await tool._poll_outgoing_once()

        assert "task-1" not in tool._pending_outgoing
        assert len(tool._completed_outgoing) == 1
        assert tool._completed_outgoing[0]["status"] == "FAILED"

    async def test_poller_start_and_cancel(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {"poll_interval": 0.1})
        tool.client = AsyncMock()
        tool.client.get_task_status = AsyncMock(
            return_value={"id": "task-1", "status": "WORKING"}
        )

        tool.start_outgoing_poller()
        assert tool._outgoing_poller_task is not None
        assert not tool._outgoing_poller_task.done()

        await tool.stop_outgoing_poller()
        assert tool._outgoing_poller_task.done() or tool._outgoing_poller_task.cancelled()
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_outgoing_poller.py -v
```

Expected: FAIL — `A2ATool` has no `_poll_outgoing_once` method.

### Step 3: Add the poller methods to A2ATool

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, add to `__init__`:

Find:

```python
        self._pending_outgoing: dict[str, dict[str, Any]] = {}
        self._completed_outgoing: list[dict[str, Any]] = []
```

Replace with:

```python
        self._pending_outgoing: dict[str, dict[str, Any]] = {}
        self._completed_outgoing: list[dict[str, Any]] = []
        self._outgoing_poller_task: asyncio.Task | None = None  # type: ignore[type-arg]
```

Add the following methods before `_resolve_url`:

```python
    async def _poll_outgoing_once(self) -> None:
        """Single poll cycle: check each pending outgoing task for terminal state."""
        terminal_states = {"COMPLETED", "FAILED", "REJECTED"}
        completed_ids: list[str] = []

        for task_id, entry in list(self._pending_outgoing.items()):
            try:
                result = await self.client.get_task_status(
                    entry["agent_url"], task_id
                )
                status = result.get("status", "")
                if status in terminal_states:
                    completed_ids.append(task_id)
                    self._completed_outgoing.append(
                        {
                            "task_id": task_id,
                            "agent_url": entry["agent_url"],
                            "agent_name": entry["agent_name"],
                            "status": status,
                            "artifacts": result.get("artifacts", []),
                            "error": result.get("error"),
                            "attribution": result.get("attribution"),
                        }
                    )
            except Exception:
                # Connection error, timeout, etc. — skip this cycle
                logger.debug("Poller: failed to reach %s for task %s", entry["agent_url"], task_id)

        for task_id in completed_ids:
            del self._pending_outgoing[task_id]

    async def _outgoing_poller_loop(self) -> None:
        """Background loop that polls pending outgoing tasks at a fixed interval."""
        interval = self.config.get("poll_interval", 5.0)
        try:
            while True:
                if self._pending_outgoing:
                    await self._poll_outgoing_once()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    def start_outgoing_poller(self) -> None:
        """Start the background outgoing poller task."""
        if self._outgoing_poller_task is None or self._outgoing_poller_task.done():
            self._outgoing_poller_task = asyncio.create_task(
                self._outgoing_poller_loop()
            )

    async def stop_outgoing_poller(self) -> None:
        """Stop the background outgoing poller task."""
        if self._outgoing_poller_task and not self._outgoing_poller_task.done():
            self._outgoing_poller_task.cancel()
            try:
                await self._outgoing_poller_task
            except asyncio.CancelledError:
                pass
```

### Step 4: Start poller in mount() and register cleanup

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, find the `mount()` function:

```python
async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> None:
    """Mount the A2A tool into the coordinator."""
    config = config or {}
    tool = A2ATool(coordinator, config)
    await coordinator.mount("tools", tool, name=tool.name)

    coordinator.register_cleanup(tool.client.close)
```

Replace with:

```python
async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> None:
    """Mount the A2A tool into the coordinator."""
    config = config or {}
    tool = A2ATool(coordinator, config)
    await coordinator.mount("tools", tool, name=tool.name)

    # Start background poller for outgoing task tracking
    tool.start_outgoing_poller()

    async def cleanup() -> None:
        await tool.stop_outgoing_poller()
        await tool.client.close()

    coordinator.register_cleanup(cleanup)
```

### Step 5: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_outgoing_poller.py -v
```

Expected: All 5 poller tests PASS.

### Step 6: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/amplifier_module_tool_a2a/__init__.py
git add amplifier-bundle-a2a/modules/tool-a2a/tests/test_outgoing_poller.py
git commit -m "feat(a2a): add background outgoing task poller"
```

---

## Task 9: Sender-Side Response Injection

Register a `provider:request` hook from `tool-a2a` that drains `_completed_outgoing` and injects response notifications into the sender's session. Includes attribution text.

**Files:**

- Modify: `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`
- Create: `modules/tool-a2a/tests/test_outgoing_injection.py`

### Step 1: Write the failing tests

Create `modules/tool-a2a/tests/test_outgoing_injection.py`:

```python
"""Tests for sender-side response injection into active session."""

from unittest.mock import AsyncMock, MagicMock


def _make_mock_registry():
    registry = MagicMock()
    registry.resolve_agent_url = MagicMock(side_effect=lambda x: x)
    registry.contact_store = MagicMock()
    registry.pending_queue = MagicMock()
    return registry


def _make_mock_coordinator(registry=None):
    coordinator = MagicMock()
    coordinator.get_capability = MagicMock(return_value=registry)
    coordinator.hooks = MagicMock()
    coordinator.register_cleanup = MagicMock()
    return coordinator


class TestOutgoingResponseInjection:
    async def test_injects_completed_response(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        tool._completed_outgoing.append(
            {
                "task_id": "task-1",
                "agent_url": "http://remote:8222",
                "agent_name": "Remote Agent",
                "status": "COMPLETED",
                "artifacts": [{"parts": [{"text": "The answer is 42"}]}],
                "attribution": "autonomous",
            }
        )

        result = await tool._handle_outgoing_responses("provider:request", {})
        assert result.action == "inject_context"
        assert "Remote Agent" in result.context_injection
        assert "The answer is 42" in result.context_injection
        assert "autonomous" in result.context_injection.lower() or "autonomously" in result.context_injection.lower()

        # Queue should be drained
        assert len(tool._completed_outgoing) == 0

    async def test_no_injection_when_empty(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        result = await tool._handle_outgoing_responses("provider:request", {})
        assert result.action == "continue"

    async def test_injects_failed_response(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        tool._completed_outgoing.append(
            {
                "task_id": "task-1",
                "agent_url": "http://remote:8222",
                "agent_name": "Remote Agent",
                "status": "FAILED",
                "artifacts": [],
                "error": "Something broke",
                "attribution": None,
            }
        )

        result = await tool._handle_outgoing_responses("provider:request", {})
        assert result.action == "inject_context"
        assert "FAILED" in result.context_injection or "failed" in result.context_injection.lower()

    async def test_injects_multiple_responses(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        tool._completed_outgoing.extend(
            [
                {
                    "task_id": "task-1",
                    "agent_url": "http://alice:8222",
                    "agent_name": "Alice",
                    "status": "COMPLETED",
                    "artifacts": [{"parts": [{"text": "Response 1"}]}],
                    "attribution": "user_response",
                },
                {
                    "task_id": "task-2",
                    "agent_url": "http://bob:8222",
                    "agent_name": "Bob",
                    "status": "COMPLETED",
                    "artifacts": [{"parts": [{"text": "Response 2"}]}],
                    "attribution": "autonomous",
                },
            ]
        )

        result = await tool._handle_outgoing_responses("provider:request", {})
        assert result.action == "inject_context"
        assert "Alice" in result.context_injection
        assert "Bob" in result.context_injection
        assert len(tool._completed_outgoing) == 0

    async def test_attribution_text_mapping(self):
        from amplifier_module_tool_a2a import A2ATool

        registry = _make_mock_registry()
        coordinator = _make_mock_coordinator(registry=registry)
        tool = A2ATool(coordinator, {})

        tool._completed_outgoing.append(
            {
                "task_id": "task-1",
                "agent_url": "http://remote:8222",
                "agent_name": "Sarah's Agent",
                "status": "COMPLETED",
                "artifacts": [{"parts": [{"text": "Yes!"}]}],
                "attribution": "escalated_user_response",
            }
        )

        result = await tool._handle_outgoing_responses("provider:request", {})
        assert result.action == "inject_context"
        # Should mention it was a personal response after escalation
        assert "personal" in result.context_injection.lower() or "escalat" in result.context_injection.lower()
```

### Step 2: Run tests to verify they fail

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_outgoing_injection.py -v
```

Expected: FAIL — `A2ATool` has no `_handle_outgoing_responses` method.

### Step 3: Add the response injection handler

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, add at the top of the file (after existing imports):

```python
from amplifier_core.models import HookResult
```

Add the following method to `A2ATool`, before `_resolve_url`:

```python
    async def _handle_outgoing_responses(
        self, event: str, data: dict[str, Any]
    ) -> HookResult:
        """Hook handler: inject completed outgoing responses into the session."""
        if not self._completed_outgoing:
            return HookResult(action="continue")

        # Drain the completed queue
        responses = list(self._completed_outgoing)
        self._completed_outgoing.clear()

        sections: list[str] = []
        for resp in responses:
            agent_name = resp.get("agent_name", "Unknown Agent")
            task_id = resp.get("task_id", "unknown")
            status = resp.get("status", "UNKNOWN")
            attribution = resp.get("attribution")

            if status == "COMPLETED":
                # Extract response text from artifacts
                text = ""
                for artifact in resp.get("artifacts", []):
                    for part in artifact.get("parts", []):
                        if isinstance(part, dict) and "text" in part:
                            text += part["text"]

                attribution_text = self._format_attribution(attribution)
                sections.append(
                    f"<a2a-response>\n"
                    f"Response from {agent_name} (task {task_id}):\n"
                    f'"{text}"\n'
                    f"({attribution_text})\n"
                    f"</a2a-response>"
                )
            elif status == "FAILED":
                error = resp.get("error", "Unknown error")
                sections.append(
                    f"<a2a-response>\n"
                    f"Request to {agent_name} (task {task_id}) failed:\n"
                    f"Error: {error}\n"
                    f"</a2a-response>"
                )
            elif status == "REJECTED":
                sections.append(
                    f"<a2a-response>\n"
                    f"Request to {agent_name} (task {task_id}) was rejected.\n"
                    f"</a2a-response>"
                )

        injection_text = "\n\n".join(sections)
        return HookResult(
            action="inject_context",
            context_injection=injection_text,
            context_injection_role="user",
            ephemeral=True,
            suppress_output=True,
        )

    @staticmethod
    def _format_attribution(attribution: str | None) -> str:
        """Convert attribution value to human-readable text."""
        mapping = {
            "autonomous": "Answered autonomously by the agent",
            "user_response": "Answered personally",
            "escalated_user_response": "Answered personally after agent escalation",
            "dismissed": "Dismissed",
        }
        return mapping.get(attribution or "", "No attribution available")
```

### Step 4: Register the hook in mount()

In `modules/tool-a2a/amplifier_module_tool_a2a/__init__.py`, find the `mount()` function. After `tool.start_outgoing_poller()`, add the hook registration:

```python
    # Register hook for injecting completed outgoing responses
    coordinator.hooks.register(
        "provider:request",
        tool._handle_outgoing_responses,
        priority=6,
        name="a2a-outgoing-delivery",
    )
```

The updated `mount()` should look like:

```python
async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> None:
    """Mount the A2A tool into the coordinator."""
    config = config or {}
    tool = A2ATool(coordinator, config)
    await coordinator.mount("tools", tool, name=tool.name)

    # Start background poller for outgoing task tracking
    tool.start_outgoing_poller()

    # Register hook for injecting completed outgoing responses
    coordinator.hooks.register(
        "provider:request",
        tool._handle_outgoing_responses,
        priority=6,
        name="a2a-outgoing-delivery",
    )

    async def cleanup() -> None:
        await tool.stop_outgoing_poller()
        await tool.client.close()

    coordinator.register_cleanup(cleanup)
```

### Step 5: Run tests

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest modules/tool-a2a/tests/test_outgoing_injection.py -v
```

Expected: All 5 injection tests PASS.

### Step 6: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 7: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/modules/tool-a2a/amplifier_module_tool_a2a/__init__.py
git add amplifier-bundle-a2a/modules/tool-a2a/tests/test_outgoing_injection.py
git commit -m "feat(a2a): add sender-side response injection via provider:request hook"
```

---

## Task 10: Update LLM Instructions

Update `context/a2a-instructions.md` to document the new `defer` operation, Mode B (live injection), automatic response delivery, and attribution.

**Files:**

- Modify: `context/a2a-instructions.md`

### Step 1: Replace the entire file

Replace the contents of `context/a2a-instructions.md` with:

```markdown
# A2A — Agent-to-Agent Communication

You have access to the `a2a` tool for communicating with remote Amplifier agents on the network.

## Operations

- **`agents`** — List all known remote agents from all sources (configured, discovered, contacts).
- **`discover`** — Browse the local network for agents via mDNS. Optional `timeout` (seconds, default 2).
- **`card`** — Fetch a remote agent's identity card. Requires `agent` (name or URL).
- **`send`** — Send a message to a remote agent. Requires `agent` and `message`. Optional `blocking` (default true), `timeout` (seconds, default 30).
- **`status`** — Check the status of a previously sent async task. Requires `agent` and `task_id`.
- **`respond`** — Reply to a pending incoming message. Requires `task_id` and `message`.
- **`defer`** — Handle a pending incoming message later. Requires `task_id`. The message won't appear again automatically but can still be responded to.
- **`dismiss`** — Dismiss a pending incoming message permanently. Requires `task_id`.
- **`approve`** — Approve a new agent requesting access. Requires `agent` (URL). Optional `tier` (default "known").
- **`block`** — Block a new agent requesting access. Requires `agent` (URL).
- **`contacts`** — List all known contacts with their trust tiers.
- **`trust`** — Change a contact's trust tier. Requires `agent` (URL) and `tier` ("trusted" or "known").

## Usage Patterns

### Sending a message
1. Call `a2a(operation="agents")` to see available agents
2. Call `a2a(operation="send", agent="Agent Name", message="your question")` to communicate
3. If the response is immediate (COMPLETED), relay it to the user
4. If INPUT_REQUIRED, tell the user — the response will be delivered automatically when available

### Receiving responses automatically
When a remote agent responds to your message, the response appears in your context automatically. You don't need to poll manually — the system delivers completed responses with attribution metadata indicating how the response was generated (autonomously by the agent, personally by the remote user, or after escalation).

### Handling incoming requests
- Pending messages and approval requests appear automatically in your context — even during an active session
- When a new message appears, you have three options:
  - `a2a(operation="respond", ...)` — reply now
  - `a2a(operation="defer", ...)` — handle later (won't re-appear until you explicitly respond)
  - `a2a(operation="dismiss", ...)` — reject permanently
- Use `a2a(operation="approve", ...)` or `a2a(operation="block", ...)` for new contact requests

### Discovery
- Call `a2a(operation="discover")` to find agents on the local network
- Agents on other networks (Tailscale, VPN) must be configured manually

## Important

- Messages are sent to remote agents on other devices — they may be controlled by other people
- Unknown agents must be approved before they can send you messages
- Trusted contacts get autonomous responses; known contacts require your input
- Incoming messages appear in real time during active sessions
- Outgoing responses are delivered automatically — no manual polling needed
- Blocking sends wait up to 30 seconds by default; use `blocking=false` for fire-and-forget
```

### Step 2: Run all tests (no code changed, just docs)

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest -v
```

Expected: All tests still pass.

### Step 3: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/context/a2a-instructions.md
git commit -m "docs(a2a): update LLM instructions with defer, Mode B, auto-delivery, attribution"
```

---

## Task 11: Integration Tests for Phase 3 Flows

End-to-end integration tests that verify the full Phase 3 feature set: live injection (Mode B), defer downgrade, attribution on all paths, and outgoing response tracking.

**Files:**

- Create: `tests/test_integration_phase3.py`

### Step 1: Write the integration tests

Create `tests/test_integration_phase3.py`:

```python
"""Phase 3 integration tests — live injection, defer, attribution, outgoing tracking."""

from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient as AioTestClient, TestServer as AioTestServer

from amplifier_module_hooks_a2a_server.card import build_agent_card
from amplifier_module_hooks_a2a_server.contacts import ContactStore
from amplifier_module_hooks_a2a_server.injection import A2AInjectionHandler
from amplifier_module_hooks_a2a_server.pending import PendingQueue
from amplifier_module_hooks_a2a_server.registry import A2ARegistry
from amplifier_module_hooks_a2a_server.server import A2AServer


async def _make_full_stack(tmp_path, contacts=None, config_overrides=None):
    """Create server + registry + stores + injection handler for integration tests."""
    config = {
        "port": 0,
        "agent_name": "Test Agent",
        "agent_description": "Test",
        "confidence_evaluation": False,
    }
    if config_overrides:
        config.update(config_overrides)

    registry = A2ARegistry()
    card = build_agent_card(config)

    contact_store = ContactStore(base_dir=tmp_path)
    pending_queue = PendingQueue(base_dir=tmp_path)
    registry.contact_store = contact_store
    registry.pending_queue = pending_queue

    injection_handler = A2AInjectionHandler(pending_queue)
    registry.injection_handler = injection_handler

    if contacts:
        for c in contacts:
            await contact_store.add_contact(
                c["url"], c["name"], c.get("tier", "known")
            )

    coordinator = MagicMock()
    coordinator.session_id = "parent-session"
    coordinator.parent_id = None
    coordinator.config = {
        "session": {"orchestrator": "loop-basic"},
        "providers": [{"module": "provider-test"}],
        "tools": [],
    }

    server = A2AServer(registry, card, coordinator, config)
    return server, registry, contact_store, pending_queue, injection_handler


class TestLiveInjectionFlow:
    """Mode B: messages appear in real time during an active session."""

    async def test_message_injected_on_next_provider_request(self, tmp_path):
        """A message arriving mid-session is injected on the next provider:request."""
        contacts = [{"url": "http://alice:8222", "name": "Alice", "tier": "known"}]
        server, registry, _, pending_queue, injection_handler = (
            await _make_full_stack(tmp_path, contacts=contacts)
        )

        # Simulate receiving a message via HTTP
        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hello!"}]},
                    "sender_url": "http://alice:8222",
                    "sender_name": "Alice",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "INPUT_REQUIRED"
            task_id = data["id"]

        # Now simulate a provider:request event
        result = await injection_handler("provider:request", {})
        assert result.action == "inject_context"
        assert "Alice" in result.context_injection
        assert "Hello!" in result.context_injection

        # Second call with no new messages — returns continue
        result2 = await injection_handler("provider:request", {})
        assert result2.action == "continue"


class TestDeferFlow:
    """Defer operation: B → A downgrade."""

    async def test_defer_then_respond(self, tmp_path):
        """User defers a message, then responds to it later."""
        contacts = [{"url": "http://alice:8222", "name": "Alice", "tier": "known"}]
        server, registry, _, pending_queue, injection_handler = (
            await _make_full_stack(tmp_path, contacts=contacts)
        )

        # Receive a message
        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Question?"}]},
                    "sender_url": "http://alice:8222",
                    "sender_name": "Alice",
                },
            )
            data = await resp.json()
            task_id = data["id"]

        # Inject it
        result = await injection_handler("provider:request", {})
        assert result.action == "inject_context"

        # Defer via tool
        from amplifier_module_tool_a2a import A2ATool

        tool_coordinator = MagicMock()
        tool_coordinator.get_capability = MagicMock(return_value=registry)
        tool = A2ATool(tool_coordinator, {})

        defer_result = await tool.execute(
            {"operation": "defer", "task_id": task_id}
        )
        assert defer_result.success is True

        # Next provider:request should NOT re-inject the deferred message
        result2 = await injection_handler("provider:request", {})
        assert result2.action == "continue"

        # Respond to the deferred message
        respond_result = await tool.execute(
            {"operation": "respond", "task_id": task_id, "message": "My answer"}
        )
        assert respond_result.success is True

        # Verify task is completed with attribution
        task = registry.get_task(task_id)
        assert task is not None
        assert task["status"] == "COMPLETED"
        assert task["attribution"] == "user_response"


class TestAttributionEndToEnd:
    """Attribution metadata set correctly on all terminal paths."""

    async def test_mode_c_autonomous_attribution(self, tmp_path):
        """Mode C success: attribution is 'autonomous'."""
        contacts = [
            {"url": "http://trusted:8222", "name": "Trusted", "tier": "trusted"}
        ]
        server, registry, _, _, _ = await _make_full_stack(
            tmp_path, contacts=contacts
        )

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value="Auto response")
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
                        "sender_url": "http://trusted:8222",
                    },
                )
                data = await resp.json()
                assert data["status"] == "COMPLETED"
                assert data["attribution"] == "autonomous"

    async def test_dismiss_attribution(self, tmp_path):
        """Dismiss: attribution is 'dismissed'."""
        contacts = [{"url": "http://alice:8222", "name": "Alice", "tier": "known"}]
        server, registry, _, _, _ = await _make_full_stack(
            tmp_path, contacts=contacts
        )

        async with AioTestClient(AioTestServer(server.app)) as client:
            resp = await client.post(
                "/a2a/v1/message:send",
                json={
                    "message": {"role": "user", "parts": [{"text": "Hello"}]},
                    "sender_url": "http://alice:8222",
                    "sender_name": "Alice",
                },
            )
            data = await resp.json()
            task_id = data["id"]

        # Dismiss via tool
        from amplifier_module_tool_a2a import A2ATool

        tool_coordinator = MagicMock()
        tool_coordinator.get_capability = MagicMock(return_value=registry)
        tool = A2ATool(tool_coordinator, {})

        await tool.execute({"operation": "dismiss", "task_id": task_id})

        task = registry.get_task(task_id)
        assert task is not None
        assert task["status"] == "REJECTED"
        assert task["attribution"] == "dismissed"

    async def test_escalated_respond_attribution(self, tmp_path):
        """C→A escalation then respond: attribution is 'escalated_user_response'."""
        contacts = [
            {"url": "http://trusted:8222", "name": "Trusted", "tier": "trusted"}
        ]
        server, registry, _, pending_queue, _ = await _make_full_stack(
            tmp_path,
            contacts=contacts,
            config_overrides={"confidence_evaluation": True},
        )

        # Mock coordinator with provider for evaluation
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value={"content": "NO"})
        server.coordinator.mount_points = {"providers": [mock_provider]}

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value="I'm unsure")
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
                        "message": {"role": "user", "parts": [{"text": "Complex Q"}]},
                        "sender_url": "http://trusted:8222",
                    },
                )
                data = await resp.json()
                assert data["status"] == "INPUT_REQUIRED"
                task_id = data["id"]

        # Verify escalated flag
        messages = pending_queue.get_pending_messages()
        assert len(messages) == 1
        assert messages[0]["escalated"] is True

        # Respond via tool
        from amplifier_module_tool_a2a import A2ATool

        tool_coordinator = MagicMock()
        tool_coordinator.get_capability = MagicMock(return_value=registry)
        tool = A2ATool(tool_coordinator, {})

        await tool.execute(
            {"operation": "respond", "task_id": task_id, "message": "The answer"}
        )

        task = registry.get_task(task_id)
        assert task is not None
        assert task["status"] == "COMPLETED"
        assert task["attribution"] == "escalated_user_response"


class TestOutgoingTrackingFlow:
    """Sender-side outgoing tracking and response injection."""

    async def test_nonblocking_send_then_poll_then_inject(self, tmp_path):
        """Non-blocking send → poller detects completion → response injected."""
        from amplifier_module_tool_a2a import A2ATool

        registry = MagicMock()
        registry.resolve_agent_url = MagicMock(side_effect=lambda x: x)
        registry.get_cached_card = MagicMock(return_value=None)
        registry.contact_store = MagicMock()
        registry.pending_queue = MagicMock()

        coordinator = MagicMock()
        coordinator.get_capability = MagicMock(return_value=registry)

        tool = A2ATool(coordinator, {"poll_interval": 0.1})
        tool.client = AsyncMock()
        tool.client.fetch_agent_card = AsyncMock(return_value={"name": "Remote"})
        tool.client.send_message = AsyncMock(
            return_value={"id": "task-1", "status": "SUBMITTED"}
        )
        tool.client.get_task_status = AsyncMock(
            return_value={
                "id": "task-1",
                "status": "COMPLETED",
                "artifacts": [{"parts": [{"text": "Done!"}]}],
                "attribution": "user_response",
            }
        )

        # Step 1: Non-blocking send
        result = await tool.execute(
            {
                "operation": "send",
                "agent": "http://remote:8222",
                "message": "Hello",
                "blocking": False,
            }
        )
        assert result.success is True
        assert "task-1" in tool._pending_outgoing

        # Step 2: Poll — detects completion
        await tool._poll_outgoing_once()
        assert "task-1" not in tool._pending_outgoing
        assert len(tool._completed_outgoing) == 1

        # Step 3: Inject response
        from amplifier_core.models import HookResult

        inject_result = await tool._handle_outgoing_responses(
            "provider:request", {}
        )
        assert inject_result.action == "inject_context"
        assert "Done!" in inject_result.context_injection
        assert len(tool._completed_outgoing) == 0
```

### Step 2: Run tests to verify they pass

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest tests/test_integration_phase3.py -v
```

Expected: All integration tests PASS (these test existing functionality from Tasks 1-9).

### Step 3: Run all tests

```bash
pytest -v
```

Expected: All tests pass.

### Step 4: Commit

```bash
cd /home/bkrabach/dev/a2a-investigate
git add amplifier-bundle-a2a/tests/test_integration_phase3.py
git commit -m "test(a2a): add Phase 3 integration tests for live injection, defer, attribution, outgoing tracking"
```

---

## Task 12: Push and Verify

Push all Phase 3 work to the remote and verify the full test suite.

**Files:** None (verification only)

### Step 1: Run the complete test suite

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a && source .venv/bin/activate
pytest -v
```

Expected: All tests pass. Count should be approximately 206 (Phase 2 baseline) + ~40 new Phase 3 tests.

### Step 2: Verify git status

```bash
cd /home/bkrabach/dev/a2a-investigate
git status
git log --oneline -15
```

Expected: Clean working directory, commits from Tasks 1-11 visible.

### Step 3: Push

```bash
git push origin main
```

### Step 4: Final verification

```bash
cd /home/bkrabach/dev/a2a-investigate/amplifier-bundle-a2a
pytest -v --tb=short
```

Expected: All tests PASS. Phase 3 is complete.
