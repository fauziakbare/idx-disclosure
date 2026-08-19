"""Stage-1 Regex Filter: fast keyword-based triage before LLM."""

from __future__ import annotations

import re
from typing import Any

# High-signal keywords for IDX disclosures (Indonesian + English)
MATERIAL_KEYWORDS = [
    r"transaksi\s+material",
    r"akuisisi",
    r"merger",
    r"konsolidasi",
    r"restrukturisasi",
    r"buyback",
    r"pembelian\s+kembali",
    r"penawaran\s+umum",
    r"ipo",
    r"rights\s+issue",
    r"hmetd",
    r"dividen",
    r"stock\s+split",
    r"reverse\s+stock",
    r"go\s+private",
    r"delisting",
    r"suspensi",
    r"pengunduran\s+diri",
    r"direktur\s+utama",
    r"komisaris\s+utama",
    r"perubahan\s+dewan",
    r"laporan\s+keuangan",
    r"rups",
    r"rapat\s+umum",
    r"material\s+transaction",
    r"acquisition",
    r"resignation",
    r"board\s+change",
    r"financial\s+report",
    r"pkpu",
    r"penundaan\s+kewajiban",
    r"afiliasi",
    r"transaksi\s+afiliasi",
    r"siaran\s+pers",
    r"press\s+release",
    r"perubahan\s+kap",
    r"kantor\s+akuntan",
    # Corporate action & contract keywords
    r"kontrak\s+baru",
    r"perolehan\s+kontrak",
    r"nilai\s+kontrak",
    r"fakta\s+material",
    r"informasi\s+material",
    r"perolehan\s+proyek",
    r"fasilitas\s+pinjaman",
    r"perjanjian",
    r"perjanjian\s+kredit",
    # Restrukturisasi & Perizinan
    r"pencabutan\s+izin",
    r"izin\s+usaha",
    r"anak\s+perusahaan",
    r"likuidasi",
    r"pembubaran",
    r"divestasi",
    # Aksi Korporasi & Modal
    r"tanpa\s+hak\s+memesan",
    r"pmthmetd",
    r"private\s+placement",
    r"tambahan\s+informasi",
    r"perubahan\s+informasi",
    # Bursa / exchange clarification patterns
    r"permintaan penjelasan",
    r"penjelasan atas (permintaan|volatilitas)",
    r"tanggapan atas permintaan penjelasan",
    r"klarifikasi atas pemberitaan",
    r"unusual market activity",
]

NOISE_PATTERNS = [
    r"laporan\s+harian.*reksa\s+dana",
    r"keterbukaan\s+informasi\s+berkala",
    r"jadwal\s+acara",
    r"pengumuman\s+rutin",
    r"daily\s+report",
    r"regular\s+disclosure",
    r"nab\s+harian",
    r"nilai\s+aktiva\s+bersih",
    r"registrasi\s+efek",
    r"bukti\s+iklan",
    r"iklan\s+media",
]

_compiled_material = [re.compile(kw, re.IGNORECASE) for kw in MATERIAL_KEYWORDS]
_compiled_noise = [re.compile(pat, re.IGNORECASE) for pat in NOISE_PATTERNS]


def stage1_filter(disclosures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter disclosure list using regex keyword matching.

    Keeps items matching material keywords, drops noise patterns.

    Args:
        disclosures: Raw disclosure dicts from fetcher.

    Returns:
        Filtered list of potentially material disclosures.
    """
    results: list[dict[str, Any]] = []

    for item in disclosures:
        title = item.get("title", "") or item.get("JudulPengumuman", "")
        attachment = item.get("Attachment", "")
        jenis = item.get("JenisPengumuman", "")
        emiten = item.get("emiten_code", "")
        text_blob = f"{title} {attachment} {jenis} {emiten}"

        # Auto-Pass: attachment name indicates material disclosure
        _auto_pass_tokens = (
            "fakta material",
            "keterbukaan informasi",
            "keterbukaan_informasi",
            "tanggapan atas permintaan penjelasan",
            "penjelasan bursa",
        )
        attachment_lower = attachment.lower()
        if any(tok in attachment_lower for tok in _auto_pass_tokens):
            item["_stage1_match"] = True
            item["_stage1_auto_pass"] = "attachment_name"
            results.append(item)
            continue

        # Skip noise first
        if any(pat.search(text_blob) for pat in _compiled_noise):
            continue

        # Keep if matches any material keyword
        if any(kw.search(text_blob) for kw in _compiled_material):
            item["_stage1_match"] = True
            results.append(item)

    return results