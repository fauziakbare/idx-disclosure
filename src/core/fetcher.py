"""In-Browser Fetch Engine.

Launch Playwright Chromium headless, open IDX announcement page to establish a
valid browser session (cookies/CAPTCHA context), then execute the
GetAnnouncement fetch directly inside the page via page.evaluate. This reuses
the browser's real TLS session + cookies to bypass WAF 403 on cloud runners.
No curl_cffi request needed for the announcement list.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.core.logger import logger

# Identical UA shared across Playwright browser and any fallback requests.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IDX_PAGE_URL = "https://www.idx.co.id/id/berita/pengumuman/"
IDX_BASE_URL = "https://www.idx.co.id"
# Default announcement API (primary /NewsAnnouncement endpoint).
IDX_API_URL = (
    "https://www.idx.co.id/primary/NewsAnnouncement/GetAnnouncement"
)

# Browser args to reduce automation fingerprinting.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--window-size=1920,1080",
]

# Shared request headers (kept for pdf_parser download compatibility).
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": IDX_PAGE_URL,
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


async def _fetch_in_browser(
    api_url: str,
    page: int,
    page_size: int,
) -> list[dict[str, Any]]:
    """Open IDX page in headless Chromium, then fetch announcements in-page."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="id-ID",
            timezone_id="Asia/Jakarta",
        )
        # Mask navigator.webdriver flag
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page_obj = await context.new_page()

        # URL with indexFrom/pageSize. Date filters intentionally omitted so the
        # endpoint's default (all announcements) is used; avoids WAF fingerprinting.
        fetch_url = f"{api_url}?indexFrom={page}&pageSize={page_size}"
        logger.info("Opening IDX announcement page in headless Chromium...")
        await page_obj.goto(
            IDX_PAGE_URL,
            wait_until="networkidle",
            timeout=60_000,
        )
        await asyncio.sleep(2)

        logger.info("Fetching announcements in-browser: %s", fetch_url)
        raw_data = await page_obj.evaluate(
            f"""
            async () => {{
                const response = await fetch('{fetch_url}', {{
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
            """
        )
        await browser.close()

    items = raw_data.get("Replies") or raw_data.get("replies") or raw_data.get("data") or []
    logger.info("Successfully fetched %d raw announcements via browser", len(items))
    return _normalize_replies(items)


async def fetch_announcements(
    page: int = 1,
    page_size: int = 30,
    date_to: str | None = None,
    base_url: str | None = None,
    date_from: str | None = None,
    emiten_code: str | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Fetch IDX announcements via in-browser evaluate (bypass WAF 403).

    Args:
        page: Page index (maps to API indexFrom).
        page_size: Number of items per page.
        date_to: Kept for backward compatibility; ignored by the browser fetch.
        base_url: Override default IDX API URL (uses primary endpoint otherwise).
        date_from: Kept for backward compatibility; ignored by the browser fetch.
        emiten_code: Ignored; announcement endpoint returns all emiten.
        **kwargs: Absorb extra caller params for forward-compatibility.

    Returns:
        List of announcement dicts from key 'Replies' / 'replies'.
    """
    api_url = (base_url if base_url else IDX_API_URL).rstrip("/")

    max_retries = len(RETRY_DELAYS)
    last_err: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            items = await _fetch_in_browser(api_url, page, page_size)
            logger.info(
                "Fetched %d announcements (in-browser attempt %d)",
                len(items),
                attempt,
            )
            return items
        except Exception as e:
            last_err = e
            logger.warning("In-browser fetch attempt %d failed: %s", attempt, e)
            if attempt < max_retries:
                backoff = RETRY_DELAYS[attempt - 1]
                logger.info("Retrying in %ds...", backoff)
                await asyncio.sleep(backoff)

    raise RuntimeError(
        f"All {max_retries} in-browser fetch attempts failed. Last error: {last_err}"
    )


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
        "jenis": pengumuman.get("JenisPengumuman", ""),
        "no_pengumuman": pengumuman.get("NoPengumuman", ""),
        "_raw": raw,
    }


def _normalize_replies(replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize all raw IDX replies to flat dicts."""
    return [_normalize_item(r) for r in replies]


# Keep backward-compatible alias
fetch_disclosures = fetch_announcements