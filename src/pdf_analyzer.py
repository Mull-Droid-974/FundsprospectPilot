"""PDF-Textextraktion mit pdfplumber."""

import os
from pathlib import Path
from typing import Optional

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from utils import logger, extract_relevant_sections


def extract_text_from_pdf(pdf_path: str) -> Optional[str]:
    """
    Extrahiert den vollständigen Text aus einer PDF-Datei.

    Returns:
        Extrahierter Text oder None bei Fehler.
    """
    if pdfplumber is None:
        raise ImportError("pdfplumber ist nicht installiert. Bitte 'pip install pdfplumber' ausführen.")

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.error(f"PDF nicht gefunden: {pdf_path}")
        return None

    try:
        full_text = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            logger.info(f"PDF geöffnet: {pdf_path.name} ({total_pages} Seiten)")

            for i, page in enumerate(pdf.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        full_text.append(page_text)
                except Exception as e:
                    logger.warning(f"Seite {i+1} konnte nicht gelesen werden: {e}")
                    continue

        if not full_text:
            logger.warning(f"Kein Text aus PDF extrahiert: {pdf_path.name}")
            return None

        combined = '\n\n'.join(full_text)
        logger.info(f"Text extrahiert: {len(combined):,} Zeichen aus {pdf_path.name}")
        return combined

    except Exception as e:
        logger.error(f"Fehler beim Lesen der PDF {pdf_path.name}: {e}")
        return None


def extract_relevant_text(pdf_path: str) -> Optional[str]:
    """
    Extrahiert den vollständigen Text und filtert die für die
    Klassifizierung relevanten Abschnitte heraus.

    Returns:
        Relevanter Text-Ausschnitt (max. ~80k Zeichen) oder None.
    """
    full_text = extract_text_from_pdf(pdf_path)
    if not full_text:
        return None

    relevant = extract_relevant_sections(full_text)
    logger.info(f"Relevanter Textausschnitt: {len(relevant):,} Zeichen")
    return relevant


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
