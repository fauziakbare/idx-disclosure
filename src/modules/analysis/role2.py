"""Role-2: Extract, Analyze, and Write structured disclosure summary."""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.llm_client import LLMClient
from src.core.logger import logger


class DisclosureType(str, Enum):
    """Klasifikasi jenis keterbukaan informasi."""

    MATERIAL_TRANSACTION = "material_transaction"
    CORPORATE_ACTION = "corporate_action"
    FINANCIAL_REPORT = "financial_report"
    BOARD_CHANGE = "board_change"
    BURSA_INQUIRY = "bursa_inquiry"
    CLARIFICATION = "clarification"
    REGULATORY_FILING = "regulatory_filing"
    GENERAL_UPDATE = "general_update"


ROLE2_SYSTEM_PROMPT = """\
Anda adalah analis senior keterbukaan informasi IDX SEKALIGUS penulis konten media sosial. Diberikan teks hasil ekstraksi dari PDF keterbukaan material, hasilkan analisis terstruktur.

1. STRICT NEGATIVE CONSTRAINTS (DILARANG KERAS)
DILARANG memuat elemen-elemen ini di summary, tweet, maupun bubble threads:
- Nomor surat birokrasi bursa (misal: "Nomor 516/TPL-P/VIII/26...", "Nomor S-10798/BEI...").
- Detail penandatangan surat (misal: "Legal & Litigation Section Head", "Corporate Secretary").
- Kalimat teaser yang menyuruh pembaca cek sendiri, seperti:
  * "Simak detailnya di keterbukaan informasi IDX"
  * "Baca selengkapnya di website bursa"
  * "Cek lampiran PDF untuk info lengkap"
- Kalimat pembuka formalitas ("Perseroan menyampaikan tanggapan resmi...").
- Tanggal pengiriman surat administratif bursa.
- Kalimat pengisi generik (misal: "Bagi investor ritel, respons cepat ini penting untuk menjaga transparansi...").
- Tag hook generik seperti [INFO REGULATOR] atau [UPDATE EMITEN].

2. ATURAN FIELD `summary` (RINGKASAN TELEGRAM)
Wajib 3-4 kalimat padat data:
- Kalimat 1: Kasus/transaksi spesifik + angka nominal konkret (misal: Transfer pricing Rp 2 Triliun, Kontrak baru Rp 1,5 Triliun).
- Kalimat 2: Tindakan/jawaban konkret manajemen (membantah, membenarkan, atau menelaah/verifikasi internal).
- Kalimat 3: Dampak hukum/operasional dan komitmen pelaporan lanjutan.
- Minimal 100 karakter. Padat fakta, tanpa basa-basi.

3. ATURAN FIELD `draft_threads` (LIST OF STRINGS: MIN 3-5 BUBBLE)
Setiap elemen array adalah 1 Bubble Threads mandiri:
- Bubble 1 (Hook & Context):
  Wajib diawali `[TAG HOOK KAPITAL SPESIFIK MATERI]` di baris pertama, diikuti konteks 1-2 kalimat, diakhiri `🧵👇`.
  Contoh: `[KLARIFIKASI BURSA: $INRU TANGGAPI ISU TRANSFER PRICING RP 2 TRILIUN]`
- Bubble 2 (Substansi & Nominal):
  Jelaskan akar persoalan/transaksi dan nominal yang terlibat secara gamblang.
- Bubble 3 (Sikap Manajemen / Strategi):
  Jelaskan posisi resmi perseroan (verifikasi internal, realisasi dana, dll).
- Bubble Penutup:
  Pertanyaan interaksi/CTA singkat + baris kepatuhan `(Disclaimer ON / Riset mandiri)`.

4. ATURAN FIELD `draft_x` (LIST OF STRINGS: MIN 3-5 TWEET)
Setiap elemen array adalah 1 Tweet mandiri (maks 280 karakter per tweet):
- Tweet 1: `[TAG HOOK KAPITAL]` + konteks ringkas + `🧵👇`.
- Tweet 2 & 3: Rincian poin fakta material, angka, dan tanggapan manajemen.
- Tweet Terakhir: Call-to-action + `(Disclaimer ON / Riset mandiri)`.

5. SCHEMA OUTPUT JSON
Output JSON dengan field berikut:
- emiten_code: Kode ticker saham
- company_name: Nama lengkap perusahaan
- disclosure_type: Kategori keterbukaan. PILIH SALAH SATU dari: "material_transaction", "corporate_action", "financial_report", "board_change", "bursa_inquiry", "clarification", "regulatory_filing", "general_update". Untuk tanggapan atas pertanyaan bursa atau klarifikasi pemberitaan, WAJIB gunakan "bursa_inquiry" atau "clarification".
- title: Judul asli keterbukaan
- summary: Ringkasan eksekutif 3-4 kalimat padat fakta dalam bahasa Indonesia (min 100 karakter)
- key_figures: Objek berisi angka metrik finansial/operasional relevan (gunakan null jika tidak ada)
- impact_assessment: "positive", "negative", atau "neutral" disertai penjelasan singkat
- risk_factors: Daftar risiko yang teridentifikasi (list kosong jika tidak ada)
- effective_date: Tanggal efektif peristiwa yang diungkapkan (YYYY-MM-DD atau null)
- draft_x: List string tweet (MIN 3, MAKS 5 tweet). Setiap tweet HARUS <= 280 karakter. Gunakan cashtag $EMITEN_CODE. Bahasa Indonesia, faktual, ringkas. Sertakan angka kunci.
- draft_threads: List string bubble naratif untuk thread (MIN 3, MAKS 5 bubble). Setiap bubble HARUS <= 500 karakter. Nada storytelling kasual bahasa Indonesia. Jelaskan MENGAPA ini penting bagi investor ritel.

6. FEW-SHOT EXAMPLE (TANGGAPAN BURSA / KLARIFIKASI)
Output HARUS mengikuti struktur dan gaya ini:

```json
{
  "disclosure_type": "bursa_inquiry",
  "summary": "INRU mengklarifikasi pemberitaan media terkait dugaan korupsi transfer pricing di lingkungan Ditjen Pajak Kemenkeu senilai Rp 2 Triliun yang melibatkan perseroan. Manajemen menyatakan baru mengetahui informasi tersebut dari media massa dan saat ini tengah melakukan penelaahan serta verifikasi internal secara mendalam. Perseroan berkomitmen menyampaikan keterbukaan lanjutan jika ditemukan fakta material yang dapat diverifikasi.",
  "draft_x": [
    "[KLARIFIKASI BURSA: $INRU BUKA SUARA SOAL DUGAAN TRANSFER PRICING RP 2 TRILIUN] 🧵👇",
    "Menanggapi isu dugaan korupsi transfer pricing di lingkungan Ditjen Pajak Kemenkeu senilai Rp 2 Triliun, manajemen $INRU menyatakan baru mengetahui kabar ini dari media massa.",
    "Saat ini $INRU sedang melakukan penelaahan dan verifikasi internal secara mendalam, serta berkomitmen merilis keterbukaan lanjutan jika ada fakta material baru.",
    "Pantau terus perkembangannya dan pastikan selalu riset mandiri! (Disclaimer ON / Riset mandiri)"
  ],
  "draft_threads": [
    "[KLARIFIKASI BURSA: $INRU TANGGAPI ISU TRANSFER PRICING RP 2 TRILIUN]\\n\\nPT Toba Pulp Lestari Tbk ($INRU) merespons permintaan penjelasan BEI terkait isu perpajakan yang menyeret nama perseroan. Poin penting penjelasannya: 🧵👇",
    "Menanggapi isu dugaan korupsi transfer pricing di Ditjen Pajak senilai Rp 2 Triliun, manajemen $INRU menyatakan baru mengetahui kabar tersebut dari media massa dan saat ini tengah melakukan proses verifikasi internal mendalam.",
    "Perseroan menegaskan akan merilis keterbukaan informasi lanjutan jika ditemukan fakta material yang terverifikasi secara hukum. Pantau terus perkembangannya!\\n\\n(Disclaimer ON / Riset mandiri)"
  ]
}
```

[CONTOH SALAH / DILARANG]:
Summary: "PT Toba Pulp Lestari Tbk menyampaikan tanggapan resmi atas surat bursa tanggal 18 Agustus 2026 melalui surat nomor 516/TPL-P. Manajemen memberikan penjelasan terkait topik transfer pricing melalui lampiran terpisah sebagai bagian dari tata kelola yang baik."

7. LARANGAN PENOMORAN DRAFT_X & DRAFT_THREADS
Setiap elemen string di list draft_x dan draft_threads DILARANG KERAS menggunakan prefix nomor urut seperti: 1., 2., 3., *1.*, *2.*, [1/4], [2/5], atau pola sejenis. Setiap elemen HARUS murni berupa teks konten tweet/thread itu sendiri tanpa awalan angka atau penomoran apa pun.

8. ATURAN UMUM
- Summary, draft_x, draft_threads HARUS dalam bahasa Indonesia
- Angka harus menyertakan satuan (IDR, %, lembar, dll)
- Jika informasi tidak tersedia, gunakan null untuk nilai tunggal atau [] untuk list
- Presisi dan faktual. Jangan berspekulasi melampaui isi dokumen.
- draft_x: setiap tweet berdiri sendiri, TANPA nomor urut/prefix angka (dilarang: 1., 2., *1.*, [1/4], dll). Murni teks konten.
- draft_threads: narasi progresif, setiap bubble membangun dari sebelumnya, TANPA nomor urut/prefix angka.
- ISI MATERI LAMPIRAN PDF HARUS DISAJIKAN SECARA LENGKAP, MENYEBUTKAN ANGKA NOMINAL, BEBAS BASA-BASI BIROKRASI, DAN TIDAK MEMBUAT KONTEN TEASER/CLICKBAIT.

9. SENTIMENT & IMPACT TAGGING (WAJIB DIISI)
Anda HARUS mengisi 3 field berikut berdasarkan analisis isi dokumen:

a) `sentiment`: PILIH SALAH SATU dari "POSITIVE", "NEUTRAL", "NEGATIVE".

b) `impact_level`: PILIH SALAH SATU dari "CRITICAL", "HIGH", "MEDIUM", "LOW", "ROUTINE".

c) `impact_tag`: PILIH SALAH SATU dari daftar berikut berdasarkan kriteria:

🚨 CRITICAL / LEGAL RISK: Kasus dugaan korupsi, sengketa pajak material, penyelidikan aparat hukum. (Sentiment: NEGATIVE, Impact: CRITICAL/HIGH).

🚨 CRITICAL / PKPU & DEFAULT: Gugatan PKPU/pailit, gagal bayar obligasi, restrukturisasi utang. (Sentiment: NEGATIVE, Impact: CRITICAL).

🟢 POSITIVE / EXPANSION: Perolehan kontrak baru besar (>10% revenue), akuisisi strategis penambah laba. (Sentiment: POSITIVE, Impact: HIGH/MEDIUM).

🟢 POSITIVE / DIVIDEND: Dividen yield jumbo/spesial, buyback saham material. (Sentiment: POSITIVE, Impact: HIGH/MEDIUM).

🟡 NEUTRAL / MATERIAL: RUPSLB, rotasi dewan direksi/komisaris, klarifikasi volatilitas saham. (Sentiment: NEUTRAL, Impact: MEDIUM).

⚪ ROUTINE / OPERATIONAL: Laporan bulanan registrasi pemegang saham, pengumuman libur bursa. (Sentiment: NEUTRAL, Impact: LOW/ROUTINE)."""

ROLE2_USER_TEMPLATE = """\
Analisis keterbukaan IDX ini. Anda HARUS merespons dengan HANYA JSON valid. Tanpa markdown, tanpa penjelasan, tanpa preamble.

Emiten Code: {emiten_code}
Title: {title}
Release Date: {release_date}

--- EXTRACTED TEXT BEGIN ---
{text}
--- EXTRACTED TEXT END ---

Output HANYA objek JSON dengan field: emiten_code, company_name, disclosure_type, title, summary, key_figures, impact_assessment, risk_factors, effective_date, sentiment, impact_level, impact_tag, draft_x, draft_threads."""


class KeyFigures(BaseModel):
    """Key financial/operational metrics from disclosure."""

    revenue: str | None = None
    net_profit: str | None = None
    total_assets: str | None = None
    transaction_value: str | None = None
    share_price: str | None = None
    shares_issued: str | None = None
    dividend_per_share: str | None = None
    other: dict[str, str] = Field(default_factory=dict)


class DisclosureAnalysis(BaseModel):
    """Structured output from Role-2 analysis."""

    emiten_code: str
    company_name: str
    disclosure_type: DisclosureType
    title: str
    summary: str
    key_figures: KeyFigures = Field(default_factory=KeyFigures)
    impact_assessment: str
    risk_factors: list[str]
    effective_date: str | None = None
    sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] = "NEUTRAL"
    impact_level: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "ROUTINE"] = "MEDIUM"
    impact_tag: Literal[
        "\U0001f6a8 CRITICAL / LEGAL RISK",
        "\U0001f6a8 CRITICAL / PKPU & DEFAULT",
        "\U0001f7e2 POSITIVE / EXPANSION",
        "\U0001f7e2 POSITIVE / DIVIDEND",
        "\U0001f7e1 NEUTRAL / MATERIAL",
        "\u26aa ROUTINE / OPERATIONAL",
    ] = "\u26aa ROUTINE / OPERATIONAL"
    draft_x: list[str] = Field(default_factory=list)
    draft_threads: list[str] = Field(default_factory=list)

    @field_validator("key_figures", mode="before")
    @classmethod
    def _coerce_key_figures(cls, v: Any) -> Any:
        if v is None:
            return KeyFigures()
        if isinstance(v, dict):
            return v
        return v

    @field_validator("impact_assessment", mode="before")
    @classmethod
    def _coerce_impact(cls, v: Any) -> str:
        if isinstance(v, dict):
            # LLM sometimes returns {"verdict": "neutral", "explanation": "..."}
            return v.get("verdict", v.get("rating", v.get("assessment", "neutral")))
        if v is None:
            return "neutral"
        return str(v)

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, v: str) -> str:
        if len(v.strip()) < 100:
            raise ValueError(
                f"summary must be at least 100 characters, got {len(v.strip())}"
            )
        return v

    @field_validator("draft_x")
    @classmethod
    def _validate_draft_x(cls, v: list[str]) -> list[str]:
        cleaned = [clean_draft_item(t) for t in v]
        if len(cleaned) < 3:
            raise ValueError(
                f"draft_x must have at least 3 items, got {len(cleaned)}"
            )
        return cleaned

    @field_validator("draft_threads")
    @classmethod
    def _validate_draft_threads(cls, v: list[str]) -> list[str]:
        cleaned = [clean_draft_item(t) for t in v]
        if len(cleaned) < 3:
            raise ValueError(
                f"draft_threads must have at least 3 items, got {len(cleaned)}"
            )
        return cleaned

    @field_validator("impact_tag", mode="before")
    @classmethod
    def _normalize_impact_tag(cls, v: Any) -> str:
        """LLM often strips emoji prefixes; re-add them."""
        if not isinstance(v, str):
            return "\u26aa ROUTINE / OPERATIONAL"
        tag = v.strip()
        _TAG_MAP = {
            "CRITICAL / LEGAL RISK": "\U0001f6a8 CRITICAL / LEGAL RISK",
            "CRITICAL / PKPU & DEFAULT": "\U0001f6a8 CRITICAL / PKPU & DEFAULT",
            "POSITIVE / EXPANSION": "\U0001f7e2 POSITIVE / EXPANSION",
            "POSITIVE / DIVIDEND": "\U0001f7e2 POSITIVE / DIVIDEND",
            "NEUTRAL / MATERIAL": "\U0001f7e1 NEUTRAL / MATERIAL",
            "ROUTINE / OPERATIONAL": "\u26aa ROUTINE / OPERATIONAL",
        }
        # Already has emoji prefix — return as-is if valid
        valid_tags = set(_TAG_MAP.values())
        if tag in valid_tags:
            return tag
        # Strip any existing emoji-like prefix and re-map
        import re as _re
        stripped = _re.sub(r"^[\U0001f6a8\U0001f7e2\U0001f7e1\u26aa]\s*", "", tag).strip()
        if stripped in _TAG_MAP:
            return _TAG_MAP[stripped]
        # Keyword fallback
        upper = stripped.upper()
        for key, val in _TAG_MAP.items():
            if key in upper:
                return val
        return "\u26aa ROUTINE / OPERATIONAL"

    @field_validator("disclosure_type", mode="before")
    @classmethod
    def _coerce_disclosure_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            # Normalize common LLM variations
            normalized = v.strip().lower().replace(" ", "_").replace("-", "_")
            try:
                return DisclosureType(normalized)
            except ValueError:
                # Fallback: map common aliases
                alias_map = {
                    "transaksi_material": "material_transaction",
                    "laporan_keuangan": "financial_report",
                    "perubahan_direksi": "board_change",
                    "aksi_korporasi": "corporate_action",
                    "pertanyaan_bursa": "bursa_inquiry",
                    "klarifikasi": "clarification",
                    "filing_regulasi": "regulatory_filing",
                    "umum": "general_update",
                }
                mapped = alias_map.get(normalized, "general_update")
                return DisclosureType(mapped)
        if isinstance(v, DisclosureType):
            return v
        return DisclosureType.GENERAL_UPDATE


_CLEAN_DRAFT_NUM = re.compile(r"^(\*?\d+[\.\)]\*?|\[\d+/\d+\])\s*")


def clean_draft_item(text: str) -> str:
    """Strip leading numbering prefixes from draft tweet/thread items."""
    return _CLEAN_DRAFT_NUM.sub("", text).strip()


def _repair_truncated_json(text: str) -> str:
    """Attempt to close truncated JSON by balancing brackets and closing strings."""
    # Count unmatched quotes (simple heuristic)
    in_string = False
    escape_next = False
    stack: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
        i += 1

    # If we're inside an unclosed string, close it
    if in_string:
        text += '"'

    # Close remaining open brackets in reverse
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"

    return text


def _sanitize_json(raw: str) -> str:
    """Clean LLM output to valid JSON. Strip markdown fences, fix common issues."""
    text = raw.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    match = fence_pattern.search(text)
    if match:
        text = match.group(1).strip()

    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return text


async def analyze_disclosure(
    client: LLMClient,
    disclosure: dict[str, Any],
    extracted_text: str,
) -> DisclosureAnalysis:
    """Run Role-2 analysis on extracted disclosure text.

    Args:
        client: LLM client configured for reasoning/analysis.
        disclosure: Original disclosure metadata dict.
        extracted_text: Text extracted from PDF by extractor module.

    Returns:
        DisclosureAnalysis with structured summary, draft_x, draft_threads.
    """
    emiten_code = disclosure.get("emiten_code", "")

    # HARD GUARD: reject empty/short text before calling LLM
    if not extracted_text or len(extracted_text.strip()) < 100:
        raise ValueError(
            f"Teks PDF untuk {emiten_code} kosong/terlalu pendek "
            f"({len(extracted_text.strip()) if extracted_text else 0} chars)! "
            f"Tidak dapat dianalisis."
        )

    # Gemini supports very large context windows; send full text up to safe limit
    max_chars = int(os.getenv("ROLE2_MAX_CHARS", "200000"))
    truncated_text = extracted_text[:max_chars]
    logger.info(
        "Passing %d chars to Role-2 (total extracted: %d, limit: %d) for %s",
        len(truncated_text),
        len(extracted_text),
        max_chars,
        emiten_code,
    )
    # Debug: log extracted text stats and preview
    logger.debug(
        "Extracted text length: %d chars | Preview (first 300): %r",
        len(extracted_text),
        extracted_text[:300],
    )
    if len(extracted_text.strip()) < 50:
        logger.warning(
            "Extracted text very short (%d chars) for %s — LLM analysis may fail or produce low-quality output",
            len(extracted_text.strip()),
            emiten_code,
        )

    user_content = ROLE2_USER_TEMPLATE.format(
        emiten_code=emiten_code,
        title=disclosure.get("title", ""),
        release_date=disclosure.get("release_date", ""),
        text=truncated_text,
    )
    messages = [
        {"role": "system", "content": ROLE2_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # --- FORENSIC DEBUG: log payload sent to LLM ---
    logger.debug(
        "[ROLE-2 DEBUG] Payload to Gemini for %s | System prompt: %d chars | User message: %d chars | PDF text: %d chars\nUser msg preview (first 300):\n%s",
        emiten_code,
        len(ROLE2_SYSTEM_PROMPT),
        len(user_content),
        len(truncated_text),
        user_content[:300],
    )

    # Enforce structured JSON output via Gemini/OpenAI-compatible response_format
    json_format = {"type": "json_object"}

    raw = ""
    for attempt in range(2):
        raw = await client.chat(
            messages,
            temperature=0.1,
            max_tokens=8192,
            response_format=json_format,
        )
        if raw.strip():
            break
        logger.warning("Role-2 empty response (attempt %d), retrying...", attempt + 1)

    # --- FORENSIC DEBUG: log raw LLM response ---
    logger.debug(
        "[ROLE-2 DEBUG] Raw response from Gemini (%d chars)\nFirst 500 chars:\n%s",
        len(raw),
        raw[:500],
    )

    if not raw.strip():
        logger.error("Role-2 returned empty after retries")
        return DisclosureAnalysis(
            emiten_code=disclosure.get("emiten_code", "UNKNOWN"),
            company_name="Unknown",
            disclosure_type=DisclosureType.GENERAL_UPDATE,
            title=disclosure.get("title", ""),
            summary="Analisis gagal: LLM mengembalikan respons kosong",
            key_figures=KeyFigures(),
            impact_assessment="neutral",
            risk_factors=[],
            effective_date=None,
            sentiment="NEUTRAL",
            impact_level="ROUTINE",
            impact_tag="\u26aa ROUTINE / OPERATIONAL",
            draft_x=["[ERROR] Analisis gagal", "LLM kosong", "Coba ulang nanti"],
            draft_threads=["[ERROR] Analisis gagal", "LLM mengembalikan respons kosong", "Silakan coba ulang nanti."],
        )

    for parse_attempt in range(3):
        try:
            cleaned = _sanitize_json(raw)
            data = json.loads(cleaned)
            return DisclosureAnalysis(**data)
        except (json.JSONDecodeError, ValueError) as e:
            if parse_attempt == 0:
                # Try repairing truncated JSON before retrying LLM
                repaired = _repair_truncated_json(cleaned)
                try:
                    data = json.loads(repaired)
                    logger.info("Role-2 parsed via JSON repair")
                    return DisclosureAnalysis(**data)
                except (json.JSONDecodeError, ValueError):
                    pass
                logger.warning("Role-2 parse failed (attempt 1), retrying LLM: %s", e)
                raw = await client.chat(
                    messages,
                    temperature=0.2,
                    max_tokens=8192,
                    response_format=json_format,
                )
                continue
            if parse_attempt == 1:
                # Try repair on retry output too
                repaired = _repair_truncated_json(_sanitize_json(raw))
                try:
                    data = json.loads(repaired)
                    logger.info("Role-2 parsed via JSON repair (retry)")
                    return DisclosureAnalysis(**data)
                except (json.JSONDecodeError, ValueError):
                    pass
            logger.error("Role-2 parse failed after retries: %s | raw: %s", e, raw[:500])
            return DisclosureAnalysis(
                emiten_code=disclosure.get("emiten_code", "UNKNOWN"),
                company_name="Unknown",
                disclosure_type=DisclosureType.GENERAL_UPDATE,
                title=disclosure.get("title", ""),
                summary=f"Analisis gagal: {e}",
                key_figures=KeyFigures(),
                impact_assessment="neutral",
                risk_factors=[],
                effective_date=None,
                sentiment="NEUTRAL",
                impact_level="ROUTINE",
                impact_tag="\u26aa ROUTINE / OPERATIONAL",
                draft_x=["[ERROR] Analisis gagal", f"Parse error: {e}", "Coba ulang nanti"],
                draft_threads=["[ERROR] Analisis gagal", f"Parse error: {e}", "Silakan coba ulang nanti."],
            )

