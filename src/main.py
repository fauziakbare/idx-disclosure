"""IDX Disclosure Monitor - Main entry point for E2E pipeline test."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from typing import Any

from src.config.settings import settings
from src.core.logger import logger, setup_logging
from src.core.db import (
    close_db,
    get_draft_threads,
    get_draft_x,
    get_financial_analysis,
    init_db,
    save_analysis,
)
from src.core.fetcher import fetch_disclosures
from src.core.llm_client import create_reasoner_client, create_triage_client, create_vision_client
from src.modules.analysis.role2 import analyze_disclosure
from src.modules.extractor.pdf_parser import extract_pdf
from src.modules.filter.stage1_regex import stage1_filter as filter_stage1
from src.modules.filter.triage import triage_batch
from src.notifier.telegram_bot import TelegramCommandBot, TelegramNotifier

setup_logging()


# --- Callback handler formatters ---

_STRIP_LEADING_NUM = re.compile(r"^(\*?\d+[\.\)]\*?|\[\d+/\d+\])\s*")


def _format_draft_x(data: Any) -> str:
    """Format draft_x list into readable message."""
    if not data:
        return "Draft X belum tersedia untuk emiten ini."
    if isinstance(data, list):
        cleaned = [_STRIP_LEADING_NUM.sub("", t.strip()) for t in data]
        return "\n\n".join(cleaned)
    return str(data)


def _format_draft_threads(data: Any) -> str:
    """Format draft_threads list into readable message."""
    if not data:
        return "Draft Threads belum tersedia untuk emiten ini."
    if isinstance(data, list):
        cleaned = [_STRIP_LEADING_NUM.sub("", t.strip()) for t in data]
        return "\n\n".join(cleaned)
    return str(data)


def _format_detail(data: Any) -> str:
    """Format full financial analysis into readable message."""
    if not data:
        return "Detail analisa belum tersedia untuk emiten ini."
    if isinstance(data, dict):
        lines = []
        if data.get("company_name"):
            lines.append(f"*Perusahaan:* {data['company_name']}")
        if data.get("disclosure_type"):
            lines.append(f"*Tipe:* {data['disclosure_type']}")
        if data.get("summary"):
            lines.append(f"\n*Ringkasan:*\n{data['summary']}")
        kf = data.get("key_figures")
        if kf and isinstance(kf, dict):
            parts = []
            for k, v in kf.items():
                if v and k != "other":
                    label = k.replace("_", " ").title()
                    parts.append(f"• {label}: {v}")
            other = kf.get("other", {})
            if isinstance(other, dict):
                for k, v in other.items():
                    if v:
                        parts.append(f"• {k}: {v}")
            if parts:
                lines.append("\n*Angka Kunci:*")
                lines.extend(parts)
        ia = data.get("impact_assessment")
        if ia:
            lines.append(f"\n*Dampak:* {ia}")
        rf = data.get("risk_factors")
        if rf and isinstance(rf, list):
            lines.append("\n*Risiko:*")
            for r in rf[:5]:
                lines.append(f"⚠ {r}")
        ed = data.get("effective_date")
        if ed:
            lines.append(f"\n*Efektif:* {ed}")
        return "\n".join(lines) if lines else "Detail analisa kosong."
    return str(data)


def _clean_draft_join(data: Any) -> str:
    """Strip leading numbering and join draft items with \n\n."""
    if isinstance(data, list):
        items = [_STRIP_LEADING_NUM.sub("", t.strip()) for t in data if t and t.strip()]
        return "\n\n".join(items)
    return str(data)


def _build_pesan_1(analysis: Any, release_date: str = "") -> str:
    """Pesan 1: ringkasan & sentimen."""
    risk_lines = "\n".join(f"\u26a0\ufe0f {r}" for r in analysis.risk_factors[:5]) or "\u26a0\ufe0f -"
    effective = release_date or analysis.effective_date or "-"
    return (
        f"[{analysis.impact_tag}]\n"
        f"\U0001f7e1 {analysis.emiten_code} \u2014 {analysis.company_name}\n"
        f"\U0001f4d1 `{analysis.disclosure_type.value}`\n"
        f"\U0001f4cc {analysis.title}\n\n"
        f"Ringkasan:\n{analysis.summary}\n\n"
        f"Faktor Risiko:\n{risk_lines}\n\n"
        f"\U0001f4c5 Efektif: {effective}"
    )


def _build_pesan_2(emiten_code: str, draft_x: Any) -> str:
    """Pesan 2: draft X dalam blok kode."""
    body = _clean_draft_join(draft_x)
    return f"\U0001f4f1 Draf X \u2014 ${emiten_code}\n\n```\n{body}\n```"


def _build_pesan_3(emiten_code: str, draft_threads: Any) -> str:
    """Pesan 3: draft Threads dalam blok kode."""
    body = _clean_draft_join(draft_threads)
    return f"\U0001f9f5 Draf Threads \u2014 ${emiten_code}\n\n```\n{body}\n```"


async def _cb_draft_x(chat_id: str, emiten_code: str) -> str:
    """Callback handler: draft X button."""
    data = await get_draft_x(emiten_code)
    header = f"🐦 *Draft X — {emiten_code}*\n\n"
    return header + _format_draft_x(data)


async def _cb_draft_threads(chat_id: str, emiten_code: str) -> str:
    """Callback handler: draft Threads button."""
    data = await get_draft_threads(emiten_code)
    header = f"🧵 *Draft Threads — {emiten_code}*\n\n"
    return header + _format_draft_threads(data)


async def _cb_detail(chat_id: str, emiten_code: str) -> str:
    """Callback handler: detail analisa button."""
    data = await get_financial_analysis(emiten_code)
    header = f"📊 *Detail Analisa — {emiten_code}*\n\n"
    return header + _format_detail(data)


async def run_pipeline(*, force: bool = False) -> None:
    """Execute full disclosure monitoring pipeline end-to-end.

    Args:
        force: If True, skip DB cache check and re-analyze all disclosures.
    """
    if force:
        logger.info("FORCE MODE: bypassing DB cache, re-analyzing all disclosures")
    logger.info("=== IDX Disclosure Monitor Pipeline Start ===")

    # Stage 0: Fetch disclosures from IDX API
    raw_disclosures = await fetch_disclosures(
        base_url=settings.IDX_API_BASE_URL,
        date_from=settings.FETCH_DATE_FROM,
        date_to=settings.FETCH_DATE_TO,
        page=settings.FETCH_INDEX_FROM,
        page_size=settings.FETCH_PAGE_SIZE,
    )
    logger.info("Fetched %d raw disclosures", len(raw_disclosures))

    if not raw_disclosures:
        logger.warning("No disclosures fetched. Check IDX_API_BASE_URL and date range.")
        return

    # Stage 1: Regex pre-filter
    stage1_results = filter_stage1(raw_disclosures)
    logger.info(
        "Stage-1 regex filter: %d passed out of %d",
        len(stage1_results),
        len(raw_disclosures),
        extra={"filter_status": "TRIAGED"},
    )

    if not stage1_results:
        logger.info("No disclosures passed Stage-1 filter. Pipeline complete.")
        return

    # Stage 2: Role-1 LLM Triage
    triage_client = create_triage_client(settings)
    material_items = await triage_batch(triage_client, stage1_results)
    logger.info("Role-1 Triage: %d material out of %d", len(material_items), len(stage1_results))

    if not material_items:
        logger.info("No material disclosures found. Pipeline complete.")
        return

    # Stage 3: PDF Extract + Role-2 Analysis
    vision_client = create_vision_client(settings)
    reasoner_client = create_reasoner_client(settings)
    notifier = TelegramNotifier(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
    )

    for item in material_items:
        item_t0 = time.monotonic()
        pdf_url = item.get("pdf_url", "")
        all_pdf_urls = item.get("all_pdf_urls", []) or ([pdf_url] if pdf_url else [])
        emiten_code = item.get("emiten_code", "UNKNOWN")
        title = item.get("title", "Untitled")
        release_date = item.get("release_date", "")

        logger.info(
            "Processing: %s - %s",
            emiten_code,
            title,
            extra={"filter_status": "ANALYZED"},
        )
        logger.info("Attachment URLs (%d): %s", len(all_pdf_urls), all_pdf_urls)

        # Extract text from ALL attachments and join
        all_texts: list[str] = []
        method = "none"
        for i, url in enumerate(all_pdf_urls):
            extraction = await extract_pdf(url, vision_client=vision_client)
            part_text = extraction["text"]
            part_method = extraction["method"]
            logger.info("Attachment %d/%d (%s): %d chars", i + 1, len(all_pdf_urls), part_method, len(part_text))
            if part_text.strip():
                all_texts.append(part_text)
            if i == 0:
                method = part_method  # primary method from first PDF

        extracted_text = "\n\n--- ATTACHMENT BREAK ---\n\n".join(all_texts)
        logger.info("Combined extraction: %d chars from %d attachments", len(extracted_text), len(all_texts))

        # HARD CHECK: refuse to call LLM with empty/short text
        if len(extracted_text.strip()) < 100:
            logger.error(
                "[ERROR] Gagal membaca teks PDF. "
                "Ekstraksi menghasilkan %d chars (threshold: 100). "
                "Melewati analisis untuk %s!",
                len(extracted_text.strip()),
                emiten_code,
            )
            continue

        # DEBUG TRACE: input to Role-2
        logger.debug(
            "DEBUG MAIN PIPELINE: %s | Judul: %s | Attachment URLs: %s | Extracted Length: %d chars\n%s",
            emiten_code,
            title,
            all_pdf_urls,
            len(extracted_text),
            extracted_text[:1000],
        )

        # Role-2 Analysis
        analysis = await analyze_disclosure(reasoner_client, item, extracted_text)

        # DEBUG TRACE: Role-2 raw result
        logger.debug("DEBUG ROLE2 RESULT DRAFT_X: %s", analysis.draft_x)

        logger.info("Analysis complete for %s: %s", emiten_code, analysis.disclosure_type)

        # Save to DB
        disclosure_id = item.get("id", f"{emiten_code}_{release_date}")
        analysis_dict = analysis.model_dump()
        await save_analysis(
            disclosure_id=disclosure_id,
            emiten_code=emiten_code,
            title=title,
            draft_x=analysis.draft_x or None,
            draft_threads=analysis.draft_threads or None,
            financial_analysis=analysis_dict,
            extracted_data={"text": extracted_text[:5000], "method": method},
            urgency_score=item.get("urgency_score"),
            is_material=True,
            filter_status="ANALYZED",
            release_date=release_date or None,
        )

        # Send 3 sequential messages per emiten
        try:
            await notifier.send_message(_build_pesan_1(analysis, release_date), parse_mode="Markdown")
            await notifier.send_message(
                _build_pesan_2(analysis.emiten_code, analysis.draft_x),
                parse_mode="Markdown",
            )
            await notifier.send_message(
                _build_pesan_3(analysis.emiten_code, analysis.draft_threads),
                parse_mode="Markdown",
            )
            logger.info(
                "Sent 3 Telegram messages for %s", emiten_code,
                extra={"duration_ms": round((time.monotonic() - item_t0) * 1000, 1)},
            )
        except Exception as e:
            logger.error("Telegram send failed for %s: %s", emiten_code, e)

    logger.info("=== Pipeline Complete ===")


async def _cmd_start(chat_id: str, args: str) -> str:
    """Handler for /start command."""
    return (
        "*IDX Disclosure Monitor Bot*\n\n"
        "Monitor otomatis pengumuman material IDX + kirim analisa ke chat ini.\n\n"
        "Perintah tersedia: `/start`, `/help`\n"
        "Tekan tombol di bawah analisa untuk draft X, Threads, atau detail.\n\n"
        "Bot berjalan dan mendengarkan perintah."
    )


async def _cmd_help(chat_id: str, args: str) -> str:
    """Handler for /help command."""
    return (
        "*Perintah Bot*\n\n"
        "- `/start` — info bot\n"
        "- `/help` — daftar perintah ini\n\n"
        "Analisa datang dengan tombol inline: 🐦 *Draf X*, 🧵 *Draf Threads*, 📊 *Detail Analisa*."
    )


async def run_bot() -> None:
    """Run Telegram command bot with callback handlers."""
    allowed_ids = [settings.TELEGRAM_CHAT_ID]
    if settings.ALLOWED_TELEGRAM_USER_ID:
        allowed_ids.append(settings.ALLOWED_TELEGRAM_USER_ID)

    bot = TelegramCommandBot(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        allowed_chat_ids=allowed_ids,
    )

    # Register command handlers
    bot.register_command("start", _cmd_start)
    bot.register_command("help", _cmd_help)

    # Register callback handlers for inline buttons
    bot.register_callback_handler("draft_x", _cb_draft_x)
    bot.register_callback_handler("draft_threads", _cb_draft_threads)
    bot.register_callback_handler("detail", _cb_detail)

    logger.info("Telegram bot started with command + callback handlers")
    await bot.poll_updates()


async def main_async(*, force: bool = False) -> None:
    """Run pipeline once for batch/CI execution.

    Args:
        force: If True, bypass DB cache and force re-analyze.
    """
    # Initialize database
    await init_db()

    try:
        # Run pipeline
        await run_pipeline(force=force)

        # Batch mode: do NOT start Telegram polling so the process exits cleanly.
        # This prevents GitHub Actions runner from hanging on an infinite loop.
        logger.info("Pipeline finished. Batch mode: skipping TelegramCommandBot.start_polling().")
    finally:
        # Close Turso client session so no unclosed client remains.
        await close_db()


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="IDX Disclosure Monitor")
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass DB cache and force re-analyze all disclosures (useful for testing)",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""
    args = _parse_args()
    try:
        asyncio.run(main_async(force=args.force))
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.critical("Pipeline fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
