"""
Native-PDF-Aufbereitung für die Anthropic-Analyse (Vision statt Text-Extraktion).

Kernidee: Die Analyse bekommt das PDF als `document`-Block (base64) — Claude
"sieht" die Seite visuell (Bilder, Grafiken, komplexe Tabellen bleiben erhalten).

Routing:
- PDF passt ins Nativ-Limit (≤600 Seiten UND ≤32 MB) → ganzes PDF nativ.
- Sonst → auf relevante Seiten REDUZIEREN (Seiten-Level-Auswahl, ganze Seiten
  wörtlich behalten) und das reduzierte PDF nativ schicken. Ergebnis wird als
  `<pdf>.reduced.pdf` gecacht.

Nur für Anbieter Anthropic (natives PDF = Anthropic-`document`-Block).
"""

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import pdfplumber
from pypdf import PdfReader, PdfWriter

from utils import logger

# pypdf/pdfminer sind bei fehlerhaften PDFs sehr geschwätzig ("wrong pointing object",
# "FontBBox" …) — auf Fehlerebene dämpfen, damit die Logs lesbar bleiben.
for _noisy in ("pypdf", "pdfminer", "pdfplumber"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# Anthropic-Request-Hardlimits für natives PDF (base64 document)
NATIVE_MAX_PAGES = 600
NATIVE_MAX_MB = 32.0
# Weiche Grenze: so viele native Seiten passen sicher ins 1M-Kontextfenster.
# Nativ ~2.000–2.500 Token/Seite (Text + Bild) → ~350 Seiten. Das ist der Trigger
# für Reduktion/Splitten (NICHT das 600er-Request-Limit — das sprengt den Kontext).
# TODO: mit count_tokens am realen Prospekt kalibrieren, sobald Guthaben da ist.
CONTEXT_PAGE_BUDGET = 350
# Sicherheitspuffer unter NATIVE_MAX_MB pro Teil-PDF
_SAFE_MB = 28.0

# Seiten-Level-Relevanz: Signalwörter (Klassifizierungs-relevante Abschnitte).
# Ausgebaut ggü. dem alten zeilenbasierten Filter — hier entscheidet er nur,
# WELCHE Seiten behalten werden; die Seite selbst geht dann komplett (nativ) rein.
_PAGE_KEYWORDS = {
    # Anleger / Zielmarkt
    "anleger", "investor", "zielmarkt", "target market", "anlegerprofil",
    "institutional", "institutionell", "retail", "privat", "kleinanleger",
    "professionell", "professional", "qualified", "qualifiziert", "semi-professional",
    # Anteilsklassen
    "anteilsklasse", "share class", "unit class", "tranche", "kategorie", "klasse",
    # Mindestanlage / Zeichnung
    "mindestanlage", "minimum investment", "mindestzeichnung", "minimum subscription",
    "erstanlage", "initial investment",
    # Gebühren / Kosten
    "gebühr", "fee", "ter", "ongoing charge", "kosten", "charges", "verwaltungsvergütung",
    "management fee", "ausgabeaufschlag",
    # Vertrieb / Dienstleistung / Restriktionen
    "vertrieb", "distribution", "placement", "verkaufsbeschränkung", "restriction",
    "mifid", "priips", "ucits", "aif", "execution only", "beratung", "mandat", "delegation",
    # Identifikatoren
    "isin", "wkn", "valor",
    # Ausschüttung / Währung
    "ausschüttung", "distribution", "thesaurierung", "accumulation", "währung", "currency",
}

_NEIGHBOR = 1  # ± Seiten um einen Treffer (Kontext / seitenübergreifende Tabellen)


# ── Metadaten (leichtgewichtig) ────────────────────────────────────────────────

def pdf_metadata_light(pdf_path: str) -> tuple[int, float]:
    """(Seitenzahl, Größe in MB) — ohne Volltext-Extraktion."""
    size_mb = os.path.getsize(pdf_path) / 1_048_576
    try:
        reader = PdfReader(pdf_path)
        pages = len(reader.pages)
    except Exception:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages = len(pdf.pages)
        except Exception:
            pages = 0
    return pages, size_mb


def fits_native(pages: int, size_mb: float) -> bool:
    return 0 < pages <= NATIVE_MAX_PAGES and size_mb <= NATIVE_MAX_MB


# ── document-Block ─────────────────────────────────────────────────────────────

def to_document_block(pdf_path: str) -> dict:
    """base64-`document`-Block für die Anthropic Messages-/Batch-API."""
    data = base64.standard_b64encode(Path(pdf_path).read_bytes()).decode("ascii")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
    }


# ── Seiten-Relevanz + Reduktion ────────────────────────────────────────────────

def _page_is_relevant(page) -> bool:
    """Prüft eine pdfplumber-Seite auf Klassifizierungs-Relevanz (Text ODER Tabelle)."""
    try:
        text = (page.extract_text() or "").lower()
    except Exception:
        text = ""
    if any(kw in text for kw in _PAGE_KEYWORDS):
        return True
    # Tabellen-Header prüfen (share class / ISIN / Gebühren-Tabellen)
    try:
        for tbl in page.extract_tables() or []:
            header = " ".join(c.lower() for row in tbl[:2] for c in row if c)
            if any(kw in header for kw in _PAGE_KEYWORDS):
                return True
    except Exception:
        pass
    return False


def relevant_page_indices(pdf_path: str) -> list[int]:
    """0-basierte Indizes der relevanten Seiten (+ Nachbarseiten). Leer = keine erkannt."""
    hits: set[int] = set()
    try:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                if _page_is_relevant(page):
                    for j in range(max(0, i - _NEIGHBOR), min(n, i + _NEIGHBOR + 1)):
                        hits.add(j)
    except Exception as e:
        logger.warning(f"Seiten-Relevanz fehlgeschlagen für {Path(pdf_path).name}: {e}")
    return sorted(hits)


def _write_subset(pdf_path: str, page_indices: list[int], out_path: str) -> float:
    """Schreibt die angegebenen Seiten als neues PDF. Gibt die Größe in MB zurück."""
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    n = len(reader.pages)
    for i in page_indices:
        if 0 <= i < n:
            writer.add_page(reader.pages[i])
    with open(out_path, "wb") as f:
        writer.write(f)
    return os.path.getsize(out_path) / 1_048_576


def _chunk_page_budget(total_pages: int, total_mb: float) -> int:
    """Seiten pro Teil, das sowohl das Kontext-Budget als auch ~28 MB einhält."""
    per_page_mb = (total_mb / total_pages) if total_pages else 0.0
    by_mb = int(_SAFE_MB / per_page_mb) if per_page_mb > 0 else CONTEXT_PAGE_BUDGET
    return max(1, min(CONTEXT_PAGE_BUDGET, by_mb))


def _make_part(block, reduced, part, n_parts, pages_in, pages_native, size_mb) -> dict:
    return {"block": block, "reduced": reduced, "split": n_parts > 1,
            "part": part, "n_parts": n_parts, "pages_in": pages_in,
            "pages_native": pages_native, "size_mb": round(size_mb, 1)}


# ── Haupteinstieg ──────────────────────────────────────────────────────────────

def prepare_pdf_documents(pdf_path: str) -> list[dict]:
    """Bereitet ein PDF als NATIVE document-Blöcke auf (Hybrid, verlustfrei):

    - passt ganz (≤ CONTEXT_PAGE_BUDGET Seiten UND ≤ NATIVE_MAX_MB) → EIN ganzer Block
    - sonst → relevante Seiten behalten (Boilerplate weg, nie eine relevante Seite kappen)
      · passen die relevanten Seiten ins Budget → EIN reduzierter Block
      · sonst → verlustfrei in mehrere native Teil-Blöcke splitten (alle relevanten
        Seiten, jeder Teil ≤ Budget/≤MB) — Ergebnisse werden pro Gruppe gemerged
    - keine relevanten Seiten erkennbar (z.B. reiner Scan) → ALLE Seiten, gesplittet

    Gibt eine Liste von Teilen zurück (meist genau 1). [] bei Fehler/unlesbar."""
    p = Path(pdf_path)
    if not p.exists():
        logger.error(f"PDF nicht gefunden: {pdf_path}")
        return []
    pages, size_mb = pdf_metadata_light(pdf_path)
    if pages == 0:
        logger.error(f"PDF nicht lesbar (0 Seiten): {p.name}")
        return []

    # 1) passt ganz nativ (Kontext-Budget)?
    if pages <= CONTEXT_PAGE_BUDGET and size_mb <= NATIVE_MAX_MB:
        return [_make_part(to_document_block(pdf_path), False, 1, 1, pages, pages, size_mb)]

    # 2) zu groß → relevante Seiten wählen (Boilerplate raus, nichts Relevantes kappen)
    rel = relevant_page_indices(pdf_path)
    lossless_all = not rel
    if lossless_all:
        rel = list(range(pages))
        logger.warning(f"[{p.name}] keine relevanten Seiten erkannt — sende ALLE {pages} Seiten "
                       f"(gesplittet, verlustfrei)")
    else:
        logger.info(f"[{p.name}] {pages} S / {size_mb:.0f} MB → {len(rel)} relevante Seiten")

    # 3) in kontext-/MB-taugliche Teile schneiden (verlustfrei, nichts wird verworfen)
    budget = _chunk_page_budget(pages, size_mb)
    chunks = [rel[i:i + budget] for i in range(0, len(rel), budget)]
    n = len(chunks)
    parts: list[dict] = []
    try:
        for k, idx in enumerate(chunks, 1):
            out = (p.with_suffix(".reduced.pdf") if n == 1
                   else p.with_suffix(f".native{k}.pdf"))
            if out.exists():
                rsize = os.path.getsize(out) / 1_048_576
            else:
                rsize = _write_subset(pdf_path, idx, str(out))
            parts.append(_make_part(to_document_block(str(out)), True, k, n, pages, len(idx), rsize))
    except Exception as e:
        logger.error(f"[{p.name}] PDF-Aufbereitung/Splitten fehlgeschlagen: {e}")
        return []

    total_native = sum(pp["pages_native"] for pp in parts)
    logger.info(f"[{p.name}] → {n} nativer Teil(e), {total_native} Seiten gesamt "
                f"({'verlustfreier Split' if lossless_all else 'relevanz-reduziert'})")
    return parts


def prepare_pdf_document(pdf_path: str) -> Optional[dict]:
    """Rückwärtskompatibel: nur der erste Teil (für Aufrufer, die einen Block wollen)."""
    parts = prepare_pdf_documents(pdf_path)
    return parts[0] if parts else None
