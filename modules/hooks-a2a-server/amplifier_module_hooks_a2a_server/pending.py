"""PendingQueue — persistent queues for pending messages and approval requests."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BASE_DIR = Path.home() / ".amplifier" / "a2a"


class PendingQueue:
    """Manages two persistent queues on disk: pending messages and pending approvals.

    Each queue is a JSON file containing a list of entry dicts.
    All mutations are write-through to disk.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or _DEFAULT_BASE_DIR
        self._messages_path = self._base_dir / "pending_messages.json"
        self._approvals_path = self._base_dir / "pending_approvals.json"
        self._lock = asyncio.Lock()
        self._messages: list[dict] = self._load(self._messages_path)
        self._approvals: list[dict] = self._load(self._approvals_path)

    def _load(self, path: Path) -> list[dict]:
        """Read a queue from disk. Returns empty list on missing/corrupt file."""
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            logger.warning("Corrupt or unreadable queue file: %s (not a list)", path)
            return []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Corrupt or unreadable queue file: %s (%s)", path, exc)
            return []

    def _save(self, path: Path, data: list[dict]) -> None:
        """Write a queue to disk, creating parent directories as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # --- Messages ---

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

    def get_pending_messages(self) -> list[dict]:
        """Return all message entries with status='pending'."""
        return [dict(m) for m in self._messages if m["status"] == "pending"]

    def get_message(self, task_id: str) -> dict | None:
        """Look up a specific message by task_id. Returns None if not found."""
        for entry in self._messages:
            if entry["task_id"] == task_id:
                return dict(entry)
        return None

    async def update_message_status(self, task_id: str, status: str) -> None:
        """Update message status. No-op if task_id not found."""
        async with self._lock:
            for entry in self._messages:
                if entry["task_id"] == task_id:
                    entry["status"] = status
                    self._save(self._messages_path, self._messages)
                    return

    # --- Approvals ---

    async def add_approval(
        self,
        task_id: str,
        sender_url: str,
        sender_name: str,
        message: dict,
    ) -> None:
        """Add an entry to pending_approvals.json."""
        async with self._lock:
            self._approvals.append(
                {
                    "task_id": task_id,
                    "sender_url": sender_url,
                    "sender_name": sender_name,
                    "message": message,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                }
            )
            self._save(self._approvals_path, self._approvals)

    def get_pending_approvals(self) -> list[dict]:
        """Return all approval entries with status='pending'."""
        return [dict(a) for a in self._approvals if a["status"] == "pending"]

    def get_approval(self, task_id: str) -> dict | None:
        """Look up a specific approval by task_id. Returns None if not found."""
        for entry in self._approvals:
            if entry["task_id"] == task_id:
                return dict(entry)
        return None

    async def update_approval_status(self, task_id: str, status: str) -> None:
        """Update approval status. No-op if task_id not found."""
        async with self._lock:
            for entry in self._approvals:
                if entry["task_id"] == task_id:
                    entry["status"] = status
                    self._save(self._approvals_path, self._approvals)
                    return
