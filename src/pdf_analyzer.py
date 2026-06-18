"""PDF-Textextraktion mit pdfplumber."""

import json
import os
from pathlib import Path
from typing import Optional

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from utils import logger, extract_relevant_sections


_MAX_PAGES = 150

_RELEVANT_TABLE_KEYWORDS = {
    "isin", "wkn", "valor",
    "mindestanlage", "minimum", "zeichnung", "subscription",
    "ter", "ongoing", "charges", "kosten", "gebühr", "fee",
    "währung", "currency",
    "anteilsklasse", "share class", "klasse", "class", "tranche", "kategorie",
    "anleger", "investor", "anlegertyp",
    "ausschüttung", "distribution", "thesaurierung",
}


def _is_relevant_table(headers: list[str]) -> bool:
    """Prüft ob eine Tabelle für die Anteilsklassen-Analyse relevant ist."""
    joined = " ".join(h.lower() for h in headers if h)
    return any(kw in joined for kw in _RELEVANT_TABLE_KEYWORDS)


def _try_ocr(pdf_path: Path, image_pages: list[int]) -> Optional[str]:
    """Versucht OCR auf Bild-Seiten. Gibt None zurück wenn Pakete fehlen."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        return None
    try:
        logger.info(f"OCR für {len(image_pages)} Bild-Seite(n) in {pdf_path.name} …")
        images = convert_from_path(
            str(pdf_path),
            first_page=min(image_pages),
            last_page=max(image_pages),
        )
        texts = [pytesseract.image_to_string(img, lang="deu+eng") for img in images]
        result = "\n\n".join(t for t in texts if t.strip())
        return result or None
    except Exception as e:
        logger.error(f"OCR fehlgeschlagen für {pdf_path.name}: {e}")
        return None


def extract_text_from_pdf(pdf_path: str, max_pages: int | None = _MAX_PAGES) -> Optional[str]:
    """
    Extrahiert den vollständigen Text aus einer PDF-Datei.
    Versucht OCR für Bild-Seiten wenn pytesseract + pdf2image installiert sind.
    max_pages=None liest alle Seiten ohne Limit (für den Trim-Schritt).
    """
    if pdfplumber is None:
        raise ImportError("pdfplumber ist nicht installiert. Bitte 'pip install pdfplumber' ausführen.")

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.error(f"PDF nicht gefunden: {pdf_path}")
        return None

    try:
        full_text = []
        image_pages = []

        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            logger.info(f"PDF geöffnet: {pdf_path.name} ({total_pages} Seiten)")
            pages_to_read = pdf.pages if max_pages is None else pdf.pages[:max_pages]
            if max_pages is not None and total_pages > max_pages:
                logger.warning(
                    f"PDF hat {total_pages} Seiten — nur erste {max_pages} werden verarbeitet: {pdf_path.name}"
                )

            for i, page in enumerate(pages_to_read):
                try:
                    page_text = page.extract_text()
                    if page_text and page_text.strip():
                        full_text.append(page_text)
                    else:
                        image_pages.append(i + 1)
                except Exception as e:
                    logger.warning(f"Seite {i+1} konnte nicht gelesen werden: {e}")
                    image_pages.append(i + 1)

        if image_pages:
            ocr_text = _try_ocr(pdf_path, image_pages)
            if ocr_text:
                full_text.append(ocr_text)
            elif not full_text:
                logger.warning(
                    f"Kein Text aus PDF extrahiert (Scan?): {pdf_path.name} — "
                    f"für OCR: pip install pytesseract pdf2image"
                )
                return None

        if not full_text:
            logger.warning(f"Kein Text aus PDF extrahiert: {pdf_path.name}")
            return None

        combined = '\n\n'.join(full_text)
        logger.info(f"Text extrahiert: {len(combined):,} Zeichen aus {pdf_path.name}")
        return combined

    except Exception as e:
        logger.error(f"Fehler beim Lesen der PDF {pdf_path.name}: {e}")
        return None


def format_extracted_json_for_prompt(data: dict) -> str:
    """Formatiert ein .extracted.json als lesbaren Text für den Analyse-LLM."""
    lines = []

    umbrella = data.get("umbrella") or {}
    if any(umbrella.values()):
        lines.append("=== UMBRELLA ===")
        for label, key in [
            ("Name",                    "name"),
            ("Rechtsform",              "rechtsform"),
            ("Domizil",                 "domizil"),
            ("Regulierung",             "regulierung"),
            ("Verwaltungsgesellschaft", "verwaltungsgesellschaft"),
            ("Allg. Anleger-Beschränkung", "allg_anleger_beschraenkung"),
        ]:
            if umbrella.get(key):
                lines.append(f"{label}: {umbrella[key]}")
        lines.append("")

    for pf in (data.get("portfolios") or []):
        lines.append(f"=== PORTFOLIO: {pf.get('name', '—')} ===")
        for label, key in [
            ("Fondstyp",           "fondstyp"),
            ("Anlageziel",         "anlageziel_kurz"),
            ("Anleger-Beschränkung", "anleger_beschraenkung"),
            ("MiFID-Kategorie",    "mifid_kategorie"),
        ]:
            if pf.get(key):
                lines.append(f"{label}: {pf[key]}")

        klassen = pf.get("anteilsklassen") or []
        if klassen:
            lines.append("ANTEILSKLASSEN:")
            for kl in klassen:
                parts = []
                if kl.get("isin"):                    parts.append(f"ISIN: {kl['isin']}")
                if kl.get("klasse_name"):             parts.append(f"Klasse: {kl['klasse_name']}")
                if kl.get("waehrung"):                parts.append(f"Währung: {kl['waehrung']}")
                if kl.get("mindestanlage"):           parts.append(f"Mindestanlage: {kl['mindestanlage']}")
                if kl.get("anlegertyp_beschraenkung"):parts.append(f"Anleger: {kl['anlegertyp_beschraenkung']}")
                if kl.get("ter"):                     parts.append(f"TER: {kl['ter']}")
                if kl.get("ausschuettung"):           parts.append(f"Ausschüttung: {kl['ausschuettung']}")
                vl = kl.get("vertrieb_laender")
                if vl:                                parts.append(f"Vertrieb: {', '.join(vl)}")
                lines.append("  - " + " | ".join(parts))
        lines.append("")

    return "\n".join(lines)


def extract_relevant_text(pdf_path: str) -> tuple[Optional[str], bool]:
    """
    Gibt (text, used_extracted: bool) zurück.
    Priorität: .extracted.json > .trimmed.txt > keyword-gefilterte PDF-Extraktion.
    used_extracted=True wenn .extracted.json genutzt wurde (→ Tables überspringen).
    """
    extracted = Path(pdf_path).with_suffix(".extracted.json")
    if extracted.exists():
        try:
            import json as _json
            data = _json.loads(extracted.read_text(encoding="utf-8"))
            text = format_extracted_json_for_prompt(data)
            logger.info(f"Extracted-JSON verwendet: {extracted.name} ({len(text):,} Zeichen)")
            return text, True
        except Exception as e:
            logger.warning(f"Fehler beim Lesen von {extracted.name}: {e} — fallback auf trimmed.txt")

    trimmed = Path(pdf_path).with_suffix(".trimmed.txt")
    if trimmed.exists():
        text = trimmed.read_text(encoding="utf-8")
        logger.info(f"Trimmed-Text verwendet: {trimmed.name} ({len(text):,} Zeichen)")
        return text, False

    full_text = extract_text_from_pdf(pdf_path)
    if not full_text:
        return None, False

    relevant = extract_relevant_sections(full_text)
    logger.info(f"Relevanter Textausschnitt: {len(relevant):,} Zeichen")
    return relevant, False


def extract_tables_from_pdf(pdf_path: str, max_pages: int | None = _MAX_PAGES) -> list[dict]:
    """
    Extrahiert alle Tabellen aus der PDF als strukturierte Liste.
    Jede Tabelle: {"page": int, "headers": [...], "rows": [[...], ...]}.
    max_pages=None liest alle Seiten ohne Limit (für den Trim-Schritt).
    """
    if pdfplumber is None:
        return []

    tables = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages_to_read = pdf.pages if max_pages is None else pdf.pages[:max_pages]
            for page_num, page in enumerate(pages_to_read, 1):
                for raw in page.extract_tables() or []:
                    if not raw or len(raw) < 2:
                        continue
                    headers = [str(h or "").strip() for h in raw[0]]
                    rows = [[str(c or "").strip() for c in row] for row in raw[1:]]
                    if any(headers) and _is_relevant_table(headers):
                        tables.append({"page": page_num, "headers": headers, "rows": rows})
    except Exception as e:
        logger.error(f"Tabellen-Extraktion fehlgeschlagen für {pdf_path}: {e}")

    return tables


def save_tables_json(pdf_path: str, tables: list[dict]) -> Path:
    """Speichert extrahierte Tabellen als .tables.json neben dem PDF."""
    out = Path(pdf_path).with_suffix(".tables.json")
    out.write_text(json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_tables_json(pdf_path: str) -> list[dict]:
    """Lädt gespeicherte Tabellen aus .tables.json falls vorhanden."""
    p = Path(pdf_path).with_suffix(".tables.json")
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def get_pdf_metadata(pdf_path: str) -> dict:
    """Gibt Metadaten der PDF zurück (Seitenanzahl, Titel etc.)."""
    if pdfplumber is None:
        return {}

    try:
        with pdfplumber.open(pdf_path) as pdf:
            meta = pdf.metadata or {}
            return {
                "pages": len(pdf.pages),
                "title": meta.get("Title", ""),
                "author": meta.get("Author", ""),
                "subject": meta.get("Subject", ""),
            }
    except Exception:
        return {}
