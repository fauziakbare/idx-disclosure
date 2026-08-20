"""PDF Extractor with PyMuPDF primary + Vision LLM fallback for scanned PDFs."""

from __future__ import annotations

import asyncio
from typing import Any

import pymupdf as fitz  # PyMuPDF (modern import; deprecated 'fitz' alias)
from curl_cffi.requests import AsyncSession

from src.core.fetcher import BASE_HEADERS, BROWSER_ARGS, USER_AGENT
from src.core.llm_client import LLMClient
from src.core.logger import logger
from src.core.proxy import get_proxy_config

# PDF download retry delays in seconds (403-gradual backoff).
PDF_RETRY_DELAYS = [3, 6, 9]

# Minimum text length to consider extraction successful
MIN_TEXT_LENGTH = 100


async def download_pdf_via_browser(pdf_url: str) -> bytes:
    """Download PDF via in-page fetch (reuses browser TLS session/cookies).

    Fallback when curl_cffi gets 403. Opens the IDX page first to establish a
    valid browser session (cf_clearance / __cf_bm), then fetches the PDF inside
    the page context and returns bytes. Avoids the 403 that a direct
    `page.goto(pdf_url)` raises ("Download is starting") on cloud WAF.
    """
    import base64

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        proxy_cfg = get_proxy_config(0)
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": BROWSER_ARGS,
        }
        if proxy_cfg:
            launch_kwargs["proxy"] = {
                "server": proxy_cfg["server"],
                **({"username": proxy_cfg["username"], "password": proxy_cfg["password"]} if "username" in proxy_cfg else {}),
            }
            logger.info("[PDF Browser] Using proxy: %s", proxy_cfg["server"])

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        # Bandwidth saver: block heavy assets
        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]
            else route.continue_(),
        )

        # Apply stealth evasions before navigating to target URL
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)

        # Establish browser session first (sets WAF cookies)
        try:
            await page.goto(
                "https://www.idx.co.id/id/berita/pengumuman/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("PDF browser page load issue (non-fatal): %s", e)

        js = f"""async () => {{
            const response = await fetch('{pdf_url}', {{
                headers: {{
                    'Accept': 'application/pdf,application/octet-stream,*/*',
                    'Referer': 'https://www.idx.co.id/id/berita/pengumuman/'
                }}
            }});
            if (!response.ok) {{
                throw new Error('In-page PDF fetch failed with status: ' + response.status);
            }}
            const buffer = await response.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binary = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {{
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }}
            return btoa(binary);
        }}"""
        b64 = await page.evaluate(js)
        await browser.close()

    return base64.b64decode(b64)


def extract_text_pymupdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyMuPDF.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        Extracted text string. Empty if extraction fails.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text: list[str] = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            if text.strip():
                pages_text.append(text)

        doc.close()
        full_text = "\n\n".join(pages_text)
        logger.info("PyMuPDF extracted %d chars from %d pages", len(full_text), len(pages_text))
        return full_text

    except Exception as e:
        logger.error("PyMuPDF extraction failed: %s", e)
        return ""


async def extract_text_vision(
    client: LLMClient,
    pdf_bytes: bytes,
    max_pages: int = 10,
) -> str:
    """Fallback: render PDF pages as images and use Vision LLM to extract text.

    Args:
        client: LLM client with vision capability.
        pdf_bytes: Raw PDF file bytes.
        max_pages: Max pages to process (to control cost).

    Returns:
        Extracted text from vision model.
    """
    import base64

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_to_process = min(len(doc), max_pages)

        all_text_parts: list[str] = []

        for page_num in range(pages_to_process):
            page = doc[page_num]
            # Render page as PNG at 200 DPI
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode("ascii")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Extract ALL text from this document page (page {page_num + 1}). "
                            "Preserve structure, tables, and numbers exactly. Output plain text.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                        },
                    ],
                }
            ]

            page_text = await client.vision_chat(messages, temperature=0.0, max_tokens=4096)
            all_text_parts.append(page_text)
            logger.debug("Vision extracted page %d: %d chars", page_num + 1, len(page_text))

        doc.close()
        full_text = "\n\n".join(all_text_parts)
        logger.info("Vision fallback extracted %d chars total", len(full_text))
        return full_text

    except Exception as e:
        logger.error("Vision extraction failed: %s", e)
        return ""


async def extract_pdf(
    pdf_url: str,
    vision_client: LLMClient | None = None,
) -> dict[str, Any]:
    """Download PDF from URL and extract text with automatic fallback.

    Primary: PyMuPDF (fast, free).
    Fallback: Vision LLM (for scanned/image-only PDFs).

    Args:
        pdf_url: URL to the PDF file.
        vision_client: Optional vision-capable LLM client for fallback.

    Returns:
        Dict with keys: text, method, pages_count.
    """
    # Download PDF as bytes using curl_cffi (TLS impersonation to bypass Cloudflare)
    pdf_bytes = b""
    if pdf_url:
        # Full chrome124 header set shared with API fetcher (UA + Referer identical).
        headers = dict(BASE_HEADERS)
        max_retries = len(PDF_RETRY_DELAYS)
        for attempt in range(max_retries):
            try:
                proxy_cfg = get_proxy_config(attempt)
                proxies = {"http": proxy_cfg["curl_url"], "https": proxy_cfg["curl_url"]} if proxy_cfg else None
                if proxy_cfg:
                    logger.info("[PDF Download Attempt %d] Using proxy: %s", attempt + 1, proxy_cfg["server"])
                async with AsyncSession(impersonate="chrome124", proxies=proxies) as session:
                    resp = await session.get(pdf_url, headers=headers, timeout=60)
                    if resp.status_code == 403:
                        raise RuntimeError("HTTP 403 Forbidden (WAF block)")
                    resp.raise_for_status()
                    pdf_bytes = resp.content
                    break
            except Exception as e:
                logger.warning("PDF download attempt %d/%d failed for %s: %s", attempt + 1, max_retries, pdf_url, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(PDF_RETRY_DELAYS[attempt])
                else:
                    # Final curl_cffi failure: fallback to Playwright browser TLS session
                    logger.error("PDF download failed after %d retries for %s; trying browser fallback", max_retries, pdf_url)
                    try:
                        pdf_bytes = await download_pdf_via_browser(pdf_url)
                        logger.info("PDF downloaded via browser fallback: %d bytes", len(pdf_bytes))
                    except Exception as browser_err:
                        logger.error("Browser PDF fallback also failed for %s: %s", pdf_url, browser_err)
                        return {"text": "", "method": "download_failed", "pages_count": 0}

    # Try PyMuPDF first
    text = extract_text_pymupdf(pdf_bytes)

    if len(text.strip()) >= MIN_TEXT_LENGTH:
        return {
            "text": text,
            "method": "pymupdf",
            "pages_count": text.count("\f") + 1,
        }

    # Fallback to Vision if available
    if vision_client is not None:
        logger.info("PyMuPDF text too short (%d chars), trying Vision fallback", len(text.strip()))
        vision_text = await extract_text_vision(vision_client, pdf_bytes)

        if len(vision_text.strip()) >= MIN_TEXT_LENGTH:
            return {
                "text": vision_text,
                "method": "vision_llm",
                "pages_count": vision_text.count("\n\n") + 1,
            }

    # Return whatever we got
    return {
        "text": text or "",
        "method": "pymupdf_partial" if text else "failed",
        "pages_count": 0,
    }