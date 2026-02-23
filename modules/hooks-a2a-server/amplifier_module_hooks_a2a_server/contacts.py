"""ContactStore — persistent contact management for A2A trust."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".amplifier" / "a2a" / "contacts.json"


class ContactStore:
    """Manages persistent contacts on disk.

    Each contact has a URL, name, trust tier, and timestamps.
    All mutations are write-through to the JSON file on disk.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._lock = asyncio.Lock()
        self._contacts: list[dict] = self._load()

    def _load(self) -> list[dict]:
        """Read contacts from disk. Returns empty list on missing/corrupt file."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            logger.warning(
                "Corrupt or unreadable contacts file: %s (not a list)", self._path
            )
            return []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Corrupt or unreadable contacts file: %s (%s)", self._path, exc
            )
            return []

    def _save(self) -> None:
        """Write contacts to disk, creating parent directories as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._contacts, indent=2),
            encoding="utf-8",
        )

    def _find(self, url: str) -> dict | None:
        """Find a contact by URL."""
        for contact in self._contacts:
            if contact["url"] == url:
                return contact
        return None

    def get_contact(self, url: str) -> dict | None:
        """Look up a contact by URL. Returns None if not found."""
        contact = self._find(url)
        if contact is None:
            return None
        return dict(contact)

    def list_contacts(self) -> list[dict]:
        """Return all contacts as a list of dicts."""
        return [dict(c) for c in self._contacts]

    def is_known(self, url: str) -> bool:
        """Quick check: is this URL in our contacts?"""
        return self._find(url) is not None

    async def add_contact(self, url: str, name: str, tier: str = "known") -> None:
        """Add a new contact. Sets first_seen and last_seen to now."""
        async with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self._contacts.append(
                {
                    "url": url,
                    "name": name,
                    "tier": tier,
                    "first_seen": now,
                    "last_seen": now,
                }
            )
            self._save()

    async def update_tier(self, url: str, tier: str) -> None:
        """Change trust tier. No-op if contact not found."""
        async with self._lock:
            contact = self._find(url)
            if contact is None:
                return
            contact["tier"] = tier
            self._save()

    async def remove_contact(self, url: str) -> None:
        """Remove a contact. No-op if not found."""
        async with self._lock:
            contact = self._find(url)
            if contact is None:
                return
            self._contacts.remove(contact)
            self._save()

    async def update_last_seen(self, url: str) -> None:
        """Update the last_seen timestamp. No-op if not found."""
        async with self._lock:
            contact = self._find(url)
            if contact is None:
                return
            contact["last_seen"] = datetime.now(timezone.utc).isoformat()
            self._save()
