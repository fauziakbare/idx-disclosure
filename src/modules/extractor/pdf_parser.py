"""PDF Extractor with PyMuPDF primary + Vision LLM fallback for scanned PDFs."""

from __future__ import annotations

import logging
from typing import Any

import fitz  # pymupdf
from curl_cffi.requests import AsyncSession

from src.core.llm_client import LLMClient

logger = logging.getLogger(__name__)

# Minimum text length to consider extraction successful
MIN_TEXT_LENGTH = 100


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
        import asyncio as _asyncio

        headers = {
            "referer": "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with AsyncSession(impersonate="chrome124") as session:
                    resp = await session.get(pdf_url, headers=headers, timeout=60)
                    resp.raise_for_status()
                    pdf_bytes = resp.content
                    break
            except Exception as e:
                logger.warning("PDF download attempt %d/%d failed for %s: %s", attempt + 1, max_retries, pdf_url, e)
                if attempt < max_retries - 1:
                    await _asyncio.sleep(2 ** (attempt + 1))
                else:
                    logger.error("PDF download failed after %d retries for %s", max_retries, pdf_url)
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