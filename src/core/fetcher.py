"""Hybrid Auto-Resolver Fetch Engine.

Playwright headless ~4s solely to harvest cookies from IDX page,
then curl_cffi (impersonate chrome124) hits official GetAnnouncement API.
No DOM scraping. No wait_for_selector on tables.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from curl_cffi.requests import AsyncSession

from src.core.logger import logger

# Identical UA shared across Playwright browser and curl_cffi requests.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IDX_PAGE_URL = "https://www.idx.co.id/id/berita/pengumuman/"
IDX_API_URL = "https://www.idx.co.id/primary/ListedCompany/GetAnnouncement"
IDX_BASE_URL = "https://www.idx.co.id"

# Browser args to reduce automation fingerprinting.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--window-size=1920,1080",
]

# Shared request headers for API + PDF downloads (chrome124 impersonation).
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

# 403-specific retry delays in seconds.
RETRY_DELAYS = [3, 6, 9]


def _normalize_date(date_str: str | None) -> str | None:
    """Convert YYYY-MM-DD to YYYYMMDD. Pass through if already YYYYMMDD."""
    if not date_str:
        return None
    # Already YYYYMMDD (8 digits, no separators)
    if re.match(r"^\d{8}$", date_str):
        return date_str
    # YYYY-MM-DD format
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}"
    # Fallback: strip non-digits
    stripped = re.sub(r"\D", "", date_str)
    return stripped if len(stripped) == 8 else None


async def _harvest_cookies() -> list[dict[str, str]]:
    """Launch Chromium headless ~4s, load IDX announcement page, return cookies."""
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
        page = await context.new_page()

        try:
            await page.goto(
                IDX_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            # Wait ~4s for session/CF cookies to fully generate
            await asyncio.sleep(4)
        except Exception as e:
            logger.warning("Playwright page load issue (non-fatal): %s", e)

        raw_cookies = await context.cookies()
        await browser.close()

    # Convert to simple name=value dicts for curl_cffi
    cookies = [{"name": c["name"], "value": c["value"]} for c in raw_cookies]
    logger.info("Harvested %d cookies via Playwright", len(cookies))
    return cookies


async def fetch_announcements(
    page: int = 1,
    page_size: int = 30,
    date_to: str | None = None,
    base_url: str | None = None,
    date_from: str | None = None,
    emiten_code: str | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Fetch IDX announcements using hybrid cookie-harvest + curl_cffi.

    Args:
        page: Page index (0-based for IDX API indexFrom).
        page_size: Number of items per page.
        date_to: End date filter YYYYMMDD. Defaults to today.
        base_url: Override default IDX API URL.
        date_from: Start date filter YYYYMMDD. Defaults to "19010101".
        **kwargs: Absorb extra caller params for forward-compatibility.

    Returns:
        List of announcement dicts from key 'replies' / 'Replies'.
    """
    # Normalize dates from settings (YYYY-MM-DD) to API format (YYYYMMDD)
    norm_date_to = _normalize_date(date_to)
    norm_date_from = _normalize_date(date_from)

    if norm_date_to is None:
        norm_date_to = datetime.now().strftime("%Y%m%d")
    if norm_date_from is None:
        norm_date_from = "19010101"

    # Use base_url from settings if provided, else fallback to hardcoded
    api_url = base_url if base_url else IDX_API_URL

    # Step 1: Harvest cookies via Playwright
    cookies = await _harvest_cookies()

    # Build cookie dict for curl_cffi
    cookie_dict = {c["name"]: c["value"] for c in cookies}

    params = {
        "kodeEmiten": emiten_code or "",
        "emitenType": "*",
        "indexFrom": str(page),
        "pageSize": str(page_size),
        "dateFrom": norm_date_from,
        "dateTo": norm_date_to,
        "lang": "id",
        "keyword": "",
    }

    # Step 2: curl_cffi request with fresh cookies
    max_retries = len(RETRY_DELAYS)
    last_err: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            async with AsyncSession(impersonate="chrome124") as session:
                resp = await session.get(
                    api_url,
                    params=params,
                    headers=BASE_HEADERS,
                    cookies=cookie_dict,
                    timeout=30,
                )
                if resp.status_code == 403:
                    raise RuntimeError("HTTP 403 Forbidden (WAF block)")
                resp.raise_for_status()
                data = resp.json()

            # Extract replies (case-insensitive key lookup)
            replies = data.get("replies") or data.get("Replies") or []
            normalized = _normalize_replies(replies)
            logger.info(
                "Fetched %d announcements (attempt %d)", len(normalized), attempt
            )
            return normalized

        except Exception as e:
            last_err = e
            logger.warning("Attempt %d failed: %s", attempt, e)
            if attempt < max_retries:
                backoff = RETRY_DELAYS[attempt - 1]
                logger.info("Retrying in %ds...", backoff)
                await asyncio.sleep(backoff)

    raise RuntimeError(
        f"All {max_retries} fetch attempts failed. Last error: {last_err}"
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
        # Ensure absolute URL
        if not raw_path.startswith(("http://", "https://")):
            raw_path = IDX_BASE_URL + (raw_path if raw_path.startswith("/") else "/" + raw_path)
        all_pdf_urls.append(raw_path)
        # Primary PDF: IsAttachment == False
        if not att.get("IsAttachment", True) and not pdf_url:
            pdf_url = raw_path

    # Fallback: first attachment as primary
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