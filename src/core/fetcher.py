"""In-Browser Fetch Engine via Playwright Network Response Interception.

Launch Playwright Chromium headless, open the IDX announcement page, and hook
the browser's network stream to capture the official GetAnnouncement JSON
response directly. Reusing the browser's real TLS session + cookies bypasses
WAF 403 on cloud runners (GitHub Actions). No curl_cffi request needed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.core.logger import logger
from src.core.proxy import get_proxy_config, get_shuffled_proxies

# Identical UA shared across Playwright browser and any fallback requests.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IDX_ANNOUNCEMENT_PAGE = "https://www.idx.co.id/id/berita/pengumuman/"
IDX_BASE_URL = "https://www.idx.co.id"
# Default announcement API (ListedCompany) proven to work from in-browser fetch.
IDX_API_URL = "https://www.idx.co.id/primary/ListedCompany/GetAnnouncement"

# Browser args to reduce automation fingerprinting.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--window-size=1920,1080",
]

# Shared request headers.
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": IDX_ANNOUNCEMENT_PAGE,
    "Origin": "https://www.idx.co.id",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# In-browser retry delays in seconds.
RETRY_DELAYS = [3, 6, 9]


def _normalize_date(date_str: str | None) -> str | None:
    """Convert YYYY-MM-DD to YYYYMMDD. Pass through if already YYYYMMDD."""
    if not date_str:
        return None
    if re.match(r"^\d{8}$", date_str):
        return date_str
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}"
    stripped = re.sub(r"\D", "", date_str)
    return stripped if len(stripped) == 8 else None


async def fetch_disclosures(limit: int = 30, **kwargs: Any) -> list[dict[str, Any]]:
    """Fetch IDX announcements by intercepting the official network response.

    Opens the IDX announcement page in headless Chromium and captures the
    GetAnnouncement JSON stream via Playwright's `page.on("response")`. This
    reuses the browser's real TLS session + cookies, bypassing WAF 403 on
    cloud runners.

    Args:
        limit: Max number of announcements to return.
        **kwargs: Absorb legacy caller params (base_url, page, page_size,
            date_from, date_to, emiten_code) for backward compatibility.

    Returns:
        List of normalized announcement dicts (Replies JSON).
    """
    from playwright.async_api import async_playwright

    captured_raw: list[dict[str, Any]] = []
    page_size = kwargs.get("page_size") or limit
    # Shuffle the proxy pool once so each retry picks a fresh, random proxy.
    proxy_pool = get_shuffled_proxies()

    async with async_playwright() as p:
        logger.info("Navigating to IDX announcement page...")
        # Up to 6 attempts; on tunnel failures the backoff lets Webshare sockets reset.
        for attempt in range(1, 7):
            # Proxy rotation: cycle through the shuffled pool per attempt.
            proxy_cfg = None
            if proxy_pool:
                proxy_cfg = proxy_pool[attempt % len(proxy_pool)]
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": BROWSER_ARGS,
            }
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_payload = {
                    "server": proxy_cfg["server"],
                }
                if "username" in proxy_cfg and "password" in proxy_cfg:
                    proxy_payload["username"] = proxy_cfg["username"]
                    proxy_payload["password"] = proxy_cfg["password"]
                logger.info(
                    "[Attempt %d] Connecting via proxy %s (Auth: %s)",
                    attempt,
                    proxy_cfg["server"],
                    "yes" if "username" in proxy_payload else "no",
                )

            browser = None
            try:
                browser = await p.chromium.launch(**launch_kwargs)
                context_kwargs = {
                    "user_agent": USER_AGENT,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "id-ID",
                    "timezone_id": "Asia/Jakarta",
                }
                if proxy_cfg and "username" in proxy_cfg and "password" in proxy_cfg:
                    context_kwargs["http_credentials"] = {
                        "username": proxy_cfg["username"],
                        "password": proxy_cfg["password"],
                    }
                context = await browser.new_context(**context_kwargs)
                # Mask navigator.webdriver flag
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = await context.new_page()

                # Bandwidth saver: block heavy assets (>80% savings)
                await page.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ["image", "media", "font", "stylesheet"]
                    else route.continue_(),
                )

                # Apply stealth evasions (mask CDP, WebGL, navigator, plugins)
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)

                # Realistic viewport & UA headers
                await page.set_extra_http_headers({
                    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                })

                # Handler to capture GetAnnouncement network responses
                async def handle_response(response: Any) -> None:
                    if "GetAnnouncement" in response.url and response.status == 200:
                        try:
                            data = await response.json()
                            replies = (
                                data.get("Replies")
                                or data.get("replies")
                                or data.get("data")
                                or []
                            )
                            if replies:
                                captured_raw.extend(replies)
                                logger.info(
                                    "Captured %d disclosures from network stream.",
                                    len(replies),
                                )
                        except Exception as e:
                            logger.debug("Failed to parse intercepted response: %s", e)

                page.on("response", handle_response)

                # domcontentloaded (NOT networkidle) — IDX streams data forever,
                # so networkidle never fires and times out.
                await page.goto(
                    IDX_ANNOUNCEMENT_PAGE,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                logger.info("Opened IDX page. Waiting for AJAX stream...")
                await asyncio.sleep(4)  # Wait for AJAX / XHR to complete

                # If the page's own stream didn't populate captured_raw
                # (e.g. different endpoint variant loaded), trigger the known
                # announcement API from within the page. This also passes through
                # page.on("response") and gets captured by our handler.
                if not captured_raw:
                    try:
                        logger.info(
                            "Triggering announcement API in-browser: %s?indexFrom=0&pageSize=%s",
                            IDX_API_URL,
                            page_size,
                        )
                        await page.evaluate(
                            f"""
                            async (pageSize) => {{
                                const response = await fetch('{IDX_API_URL}?indexFrom=0&pageSize=' + pageSize, {{
                                    headers: {{
                                        'Accept': 'application/json, text/plain, */*',
                                        'X-Requested-With': 'XMLHttpRequest'
                                    }}
                                }});
                                if (!response.ok) {{
                                    throw new Error('In-browser fetch failed with status: ' + response.status);
                                }}
                                return await response.json();
                            }}
                            """,
                            page_size,
                        )
                        await asyncio.sleep(2)  # Let the response handler capture it
                    except Exception as e:
                        logger.debug("In-browser API trigger failed: %s", e)

                if captured_raw:
                    logger.info(
                        "Network stream captured %d items on attempt %d",
                        len(captured_raw),
                        attempt,
                    )
                    if len(captured_raw) >= page_size:
                        break
                    # Trigger another fetch to fill page_size (pagination fill)
                    try:
                        extra = await page.evaluate(
                            f"""
                            async (pageSize) => {{
                                const response = await fetch('{IDX_API_URL}?indexFrom=1&pageSize=' + pageSize, {{
                                    headers: {{
                                        'Accept': 'application/json, text/plain, */*',
                                        'X-Requested-With': 'XMLHttpRequest'
                                    }}
                                }});
                                if (!response.ok) {{
                                    throw new Error('In-browser fetch failed with status: ' + response.status);
                                }}
                                return await response.json();
                            }}
                            """,
                            page_size,
                        )
                        extra_replies = (
                            extra.get("Replies")
                            or extra.get("replies")
                            or extra.get("data")
                            or []
                        )
                        if extra_replies:
                            captured_raw.extend(extra_replies)
                            logger.info(
                                "Extended capture with %d additional items.",
                                len(extra_replies),
                            )
                        break
                    except Exception as e:
                        logger.debug("In-page pagination fill failed: %s", e)
                    break

                logger.warning("Attempt %d: No data intercepted yet, retrying trigger...", attempt)
                await page.reload(wait_until="domcontentloaded", timeout=60_000)
                await asyncio.sleep(4)
            except Exception as e:
                logger.warning(
                    "Attempt %d navigation/tunnel error: %s (ERR_TUNNEL_CONNECTION_FAILED / timeout likely)",
                    attempt,
                    e,
                )
                # Backoff before next retry so Webshare socket / rate limit resets.
                await asyncio.sleep(3 * (attempt + 1))
            finally:
                # ALWAYS release browser to avoid lingering connections.
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception as e:
                        logger.debug("Error closing browser: %s", e)

            if captured_raw:
                break

    if not captured_raw:
        raise RuntimeError(
            "Failed to intercept IDX announcements via Playwright network stream."
        )

    # Deduplicate by announcement id if present, keep first occurrence order
    seen: set[str] = set()
    unique_raw: list[dict[str, Any]] = []
    for item in captured_raw:
        pengumuman = item.get("pengumuman") or {}
        key = str(pengumuman.get("Id2") or pengumuman.get("NoPengumuman") or id(item))
        if key in seen:
            continue
        seen.add(key)
        unique_raw.append(item)

    normalized = _normalize_replies(unique_raw)
    logger.info(
        "Returning %d disclosures (intercepted via network stream)",
        len(normalized[:limit]),
    )
    return normalized[:limit]


async def fetch_announcements(
    page: int = 1,
    page_size: int = 30,
    date_to: str | None = None,
    base_url: str | None = None,
    date_from: str | None = None,
    emiten_code: str | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper. Routes to network-interception fetch.

    Legacy param names are absorbed; `page_size` maps to `limit`.
    """
    return await fetch_disclosures(
        limit=page_size,
        base_url=base_url,
        page=page,
        page_size=page_size,
        date_from=date_from,
        date_to=date_to,
        emiten_code=emiten_code,
        **kwargs,
    )


# Keep backward-compatible alias: fetch_disclosures is the primary entrypoint.
# Note: fetch_disclosures is now the canonical implementation above;
# fetch_announcements delegates to it. Do NOT reassign this alias.


def _normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested IDX API response into canonical dict for pipeline."""
    pengumuman = raw.get("pengumuman") or {}
    attachments = raw.get("attachments") or []

    # Collect ALL PDF attachment URLs (not just primary)
    all_pdf_urls: list[str] = []
    pdf_url = ""
    for att in attachments:
        raw_path = att.get("FullSavePath", "")
        if not raw_path:
            continue
        if not raw_path.startswith(("http://", "https://")):
            raw_path = IDX_BASE_URL + (raw_path if raw_path.startswith("/") else "/" + raw_path)
        all_pdf_urls.append(raw_path)
        if not att.get("IsAttachment", True) and not pdf_url:
            pdf_url = raw_path

    if not pdf_url and all_pdf_urls:
        pdf_url = all_pdf_urls[0]

    emiten_code = (pengumuman.get("Kode_Emiten") or "").strip()
    title = pengumuman.get("JudulPengumuman") or ""

    return {
        "title": title,
        "emiten_code": emiten_code,
        "pdf_url": pdf_url,
        "all_pdf_urls": all_pdf_urls,
        "tanggal": pengumuman.get("TglPengumuman", ""),
        "release_date": pengumuman.get("TglPengumuman", ""),
        "jenis": pengumuman.get("JenisPengumuman", ""),
        "no_pengumuman": pengumuman.get("NoPengumuman", ""),
        "_raw": raw,
    }


def _normalize_replies(replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize all raw IDX replies to flat dicts."""
    return [_normalize_item(r) for r in replies]