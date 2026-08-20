"""Dynamic Webshare proxy rotation & caching for IDX Fetcher and PDF Parser."""

from __future__ import annotations

import random
import urllib.request
from typing import Dict, List, Optional

from src.config.settings import settings
from src.core.logger import logger

_CACHED_PROXIES: List[Dict[str, str]] = []


def parse_proxy_line(line: str) -> dict | None:
    """Strictly parse a proxy line in `ip:port:username:password` format.

    Returns a proxy config dict, or None for empty lines, comment lines, or
    lines that do not match the expected 2- or 4-part format.
    """
    clean_line = line.strip()
    if not clean_line or clean_line.startswith("#"):
        return None
    parts = clean_line.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": pwd,
            "curl_url": f"http://{user}:{pwd}@{ip}:{port}",
            "ip": ip,
            "port": port,
        }
    elif len(parts) == 2:
        ip, port = parts
        return {
            "server": f"http://{ip}:{port}",
            "curl_url": f"http://{ip}:{port}",
            "ip": ip,
            "port": port,
        }
    return None


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
            for line in content.splitlines():
                proxy_cfg = parse_proxy_line(line)
                if proxy_cfg is not None:
                    proxies.append(proxy_cfg)
            _CACHED_PROXIES = proxies
            logger.info("Loaded %d proxies from Webshare URL.", len(_CACHED_PROXIES))
            return _CACHED_PROXIES
    except Exception as e:
        logger.warning("Failed to fetch proxy list from URL: %s", e)
        return []


def get_shuffled_proxies() -> List[Dict[str, str]]:
    """Return a shuffled copy of the cached proxy pool."""
    proxies = load_proxies().copy()
    random.shuffle(proxies)
    return proxies


def get_proxy_config(attempt: int = 0) -> Optional[Dict[str, str]]:
    """Get proxy config by attempt number (rotation/failover)."""
    proxies = load_proxies()
    if not proxies:
        return None
    return proxies[attempt % len(proxies)]