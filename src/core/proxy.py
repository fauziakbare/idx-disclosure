"""Dynamic Webshare proxy rotation & caching for IDX Fetcher and PDF Parser."""

from __future__ import annotations

import urllib.request
from typing import Dict, List, Optional

from src.config.settings import settings
from src.core.logger import logger

_CACHED_PROXIES: List[Dict[str, str]] = []


def load_proxies() -> List[Dict[str, str]]:
    """Download and parse plain text proxy list from Webshare URL."""
    global _CACHED_PROXIES
    if _CACHED_PROXIES:
        return _CACHED_PROXIES

    if not settings.PROXY_LIST_URL:
        return []

    try:
        req = urllib.request.Request(
            settings.PROXY_LIST_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            proxies: List[Dict[str, str]] = []
            for line in content.strip().splitlines():
                parts = line.strip().split(":")
                if len(parts) == 4:
                    ip, port, user, pwd = parts
                    proxies.append({
                        "server": f"http://{ip}:{port}",
                        "username": user,
                        "password": pwd,
                        "curl_url": f"http://{user}:{pwd}@{ip}:{port}",
                    })
                elif len(parts) == 2:
                    ip, port = parts
                    proxies.append({
                        "server": f"http://{ip}:{port}",
                        "curl_url": f"http://{ip}:{port}",
                    })
            _CACHED_PROXIES = proxies
            logger.info("Loaded %d proxies from Webshare URL.", len(_CACHED_PROXIES))
            return _CACHED_PROXIES
    except Exception as e:
        logger.warning("Failed to fetch proxy list from URL: %s", e)
        return []


def get_proxy_config(attempt: int = 0) -> Optional[Dict[str, str]]:
    """Get proxy config by attempt number (rotation/failover)."""
    proxies = load_proxies()
    if not proxies:
        return None
    return proxies[attempt % len(proxies)]