"""A2A HTTP client — fetches Agent Cards, sends messages, polls tasks."""

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class A2AClient:
    """HTTP client for the A2A protocol.

    Handles:
    - Fetching Agent Cards (GET /.well-known/agent.json)
    - Sending messages (POST /a2a/v1/message:send)
    - Polling task status (GET /a2a/v1/tasks/{task_id})
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self.default_timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the underlying HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.default_timeout)
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def fetch_agent_card(self, base_url: str) -> dict[str, Any]:
        """Fetch a remote agent's Agent Card.

        Args:
            base_url: The agent's base URL (e.g., "http://agent.local:8222")

        Returns:
            The Agent Card as a dict.

        Raises:
            ConnectionError: If the request fails or returns non-200.
        """
        url = f"{base_url.rstrip('/')}/.well-known/agent.json"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ConnectionError(
                        f"Failed to fetch agent card from {url}: HTTP {resp.status}"
                    )
                return await resp.json()
        except aiohttp.ClientError as e:
            raise ConnectionError(f"Connection failed to {url}: {e}") from e

    async def send_message(
        self,
        base_url: str,
        message_text: str,
        timeout: float | None = None,
        sender_url: str | None = None,
        sender_name: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a remote agent.

        Args:
            base_url: The agent's base URL.
            message_text: The message text to send.
            timeout: Override the default timeout (seconds).
            sender_url: Our A2A server URL (for contact identification).
            sender_name: Our agent name (for display on the receiving end).

        Returns:
            The A2A Task response dict.

        Raises:
            ConnectionError: If the server returns 5xx or the connection fails.
        """
        url = f"{base_url.rstrip('/')}/a2a/v1/message:send"
        payload: dict[str, Any] = {
            "message": {
                "role": "user",
                "parts": [{"text": message_text}],
            }
        }
        if sender_url:
            payload["sender_url"] = sender_url
        if sender_name:
            payload["sender_name"] = sender_name

        session = await self._get_session()
        request_timeout = aiohttp.ClientTimeout(total=timeout or self.default_timeout)

        try:
            async with session.post(url, json=payload, timeout=request_timeout) as resp:
                if resp.status >= 500:
                    # Don't assume JSON — read text safely
                    error_text = await resp.text()
                    raise ConnectionError(
                        f"Server error from {url}: HTTP {resp.status} - {error_text[:200]}"
                    )
                return await resp.json()
        except aiohttp.ClientError as e:
            raise ConnectionError(f"Connection failed to {url}: {e}") from e

    async def get_task_status(self, base_url: str, task_id: str) -> dict[str, Any]:
        """Poll the status of a remote task.

        Args:
            base_url: The agent's base URL.
            task_id: The task ID to poll.

        Returns:
            The task status dict.

        Raises:
            ValueError: If the task is not found (404).
            ConnectionError: On connection failure.
        """
        url = f"{base_url.rstrip('/')}/a2a/v1/tasks/{task_id}"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    raise ValueError(f"Task not found: {task_id}")
                if resp.status >= 400:
                    raise ConnectionError(
                        f"Error polling task {task_id} from {url}: HTTP {resp.status}"
                    )
                return await resp.json()
        except aiohttp.ClientError as e:
            raise ConnectionError(f"Connection failed to {url}: {e}") from e
