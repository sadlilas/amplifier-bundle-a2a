"""mDNS service browsing via Zeroconf — discovers A2A agents on the LAN."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from zeroconf import ServiceBrowser, Zeroconf  # pyright: ignore[reportMissingImports]

    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

SERVICE_TYPE = "_a2a._tcp.local."


class _BrowseListener:
    """Collects discovered services."""

    def __init__(self) -> None:
        self.found: list[dict[str, Any]] = []

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            props: dict[str, Any] = {}
            if info.properties:
                props = {
                    k.decode() if isinstance(k, bytes) else k: v.decode()
                    if isinstance(v, bytes)
                    else v
                    for k, v in info.properties.items()
                }
            self.found.append(
                {
                    "name": props.get("name", name.split(".")[0]),
                    "url": props.get("url", ""),
                    "source": "mdns",
                }
            )

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        pass


async def browse_mdns(timeout: float = 2.0) -> list[dict[str, Any]]:
    """Browse the local network for A2A agents via mDNS.

    Args:
        timeout: How long to listen for services (seconds). Default: 2.0

    Returns:
        List of discovered agents: [{"name": ..., "url": ..., "source": "mdns"}]
        Returns empty list if zeroconf is unavailable.
    """
    if not ZEROCONF_AVAILABLE:
        logger.info("mDNS browsing skipped — zeroconf not installed")
        return []

    try:
        zc = Zeroconf()  # type: ignore[reportPossiblyUnbound]
        listener = _BrowseListener()
        browser = ServiceBrowser(zc, SERVICE_TYPE, listener)  # type: ignore[reportPossiblyUnbound]

        await asyncio.sleep(timeout)

        browser.cancel()
        zc.close()

        return listener.found
    except Exception as e:
        logger.warning("mDNS browsing failed: %s", e)
        return []
