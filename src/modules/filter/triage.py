"""Role-1 Triage LLM: evaluate ambiguous disclosures, assign urgency score."""

from __future__ import annotations

import json
from typing import Any, Protocol

from src.core.logger import logger

TRIAGE_SYSTEM_PROMPT = """\
You are an IDX disclosure triage analyst. Evaluate whether a disclosure is material for investors.

Score 1-5:
1 = Routine/noise (NAB, registrasi efek, iklan)
2 = Minor administrative update
3 = Potentially material (requires investor attention)
4 = Clearly material (transaction, board change, financial report)
5 = Critical/urgent (PKPU, suspension, major acquisition, fraud)

Tanggapan atas permintaan penjelasan bursa atau volatilitas transaksi bernilai urgensi minimal skor 3-4 jika menyangkut kelangsungan usaha, volatilitas harga ekstrem, rumor akuisisi/divestasi, atau default utang.

Respond ONLY with valid JSON:
{"urgency_score": <int>, "reason": "<brief reason>"}
"""


class TriageClient(Protocol):
    async def chat(self, messages: list[dict[str, str]]) -> str: ...


def _parse_triage_response(raw: str) -> dict[str, Any]:
    """Parse triage JSON response with fallback."""
    text = raw.strip()
    # Strip markdown code fences
    if "```" in text:
        lines = text.split("\n")
        cleaned = []
        inside = False
        for line in lines:
            if line.strip().startswith("```"):
                inside = not inside
                continue
            if inside:
                cleaned.append(line)
        text = "\n".join(cleaned).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Triage JSON parse failed, defaulting to score=0: %s", text[:200])
        return {"urgency_score": 0, "reason": "parse_error"}


async def triage_batch(
    llm_client: TriageClient,
    items: list[dict[str, Any]],
    min_score: int = 3,
) -> list[dict[str, Any]]:
    """Evaluate batch of disclosures via Role-1 Triage LLM.

    Args:
        llm_client: Client with async chat(messages) method.
        items: Disclosures that passed Stage-1 regex filter.
        min_score: Minimum urgency_score to pass (default 3).

    Returns:
        Items with urgency_score >= min_score, enriched with triage metadata.
    """
    results: list[dict[str, Any]] = []

    for item in items:
        title = item.get("title", "")
        emiten = item.get("emiten_code", "UNKNOWN")

        user_msg = f"Emiten: {emiten}\nJudul: {title}"

        try:
            raw_resp = await llm_client.chat([
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            parsed = _parse_triage_response(raw_resp)
            score = int(parsed.get("urgency_score", 0))
            reason = parsed.get("reason", "")

            item["_triage_score"] = score
            item["_triage_reason"] = reason

            if score >= min_score:
                results.append(item)
                logger.info("PASS [%d] %s - %s (%s)", score, emiten, title, reason)
            else:
                logger.debug("SKIP [%d] %s - %s (%s)", score, emiten, title, reason)

        except Exception as e:
            logger.error("Triage failed for %s: %s", emiten, e)
            # Fail-open: skip item on error
            continue

    return results