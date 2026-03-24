"""
Batch-Verarbeitung: Excel lesen → PDF holen → Claude analysieren → Ergebnis schreiben.
Wird vom GUI (app.py) in einem separaten Thread aufgerufen.
"""

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from claude_classifier import classify_prospectus
from excel_handler import (
    count_total_rows,
    iter_unprocessed_isins,
    write_error,
    write_result,
)
from fundinfo_client import fetch_prospectus
from pdf_analyzer import extract_relevant_text
from utils import logger
from web_search import search_fund_info

load_dotenv()


# ─── Konfiguration ────────────────────────────────────────────────
@dataclass
class Config:
    excel_path: str = "data/input/fonds_universe.xlsx"
    pdf_folder: str = "data/prospectus"
    batch_size: int = 200
    skip_done: bool = True
    request_delay: float = 1.5
    max_retries: int = 3
    api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            excel_path=os.getenv("EXCEL_PATH", cls.excel_path),
            pdf_folder=os.getenv("PDF_FOLDER", cls.pdf_folder),
            batch_size=int(os.getenv("BATCH_SIZE", cls.batch_size)),
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        )


# ─── Progress-Events für die GUI ──────────────────────────────────
@dataclass
class ProgressEvent:
    """Event das an die GUI gesendet wird."""
    type: str   # "progress", "log", "done", "error", "result"
    message: str = ""
    isin: str = ""
    result: dict = field(default_factory=dict)
    current: int = 0
    total: int = 0


# ─── Einzelne ISIN verarbeiten ────────────────────────────────────
def process_single_pdf(
    pdf_path: str,
    isin: str = "",
    fund_name: str = "",
    api_key: str = "",
) -> dict:
    """
    Analysiert eine einzelne PDF-Datei (für den GUI-Prototyp-Modus).

    Returns:
        Klassifizierungsergebnis als Dict.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")

    logger.info(f"Analysiere PDF: {pdf_path}")
    text = extract_relevant_text(pdf_path)

    if not text:
        return {
            "segmentierung": "fehler",
            "fondstyp": "",
            "anlegertyp": "",
            "kundentyp": "",
            "begruendung": "Kein Text aus PDF extrahierbar",
            "konfidenz": "niedrig",
        }

    # Falls Konfidenz niedrig → Web-Suche als Ergänzung
    result = classify_prospectus(text, isin=isin, fund_name=fund_name, api_key=api_key)

    if result.get("konfidenz") == "niedrig" and isin:
        logger.info(f"Konfidenz niedrig für {isin}, starte Web-Suche...")
        web_info = search_fund_info(isin, fund_name)
        if web_info:
            result = classify_prospectus(
                text, isin=isin, fund_name=fund_name,
                additional_context=web_info, api_key=api_key
            )

    return result


# ─── Batch-Verarbeitung ───────────────────────────────────────────
class BatchProcessor:
    """Verarbeitet ISINs aus einer Excel-Datei im Hintergrund-Thread."""

    def __init__(self, config: Config, progress_queue: queue.Queue):
        self.config = config
        self.q = progress_queue
        self._stop_event = threading.Event()

    def stop(self):
        """Stoppt die Verarbeitung nach der aktuellen ISIN."""
        self._stop_event.set()

    def _emit(self, event: ProgressEvent):
        """Sendet ein Event an die GUI."""
        self.q.put(event)

    def run(self):
        """Hauptschleife — läuft in einem separaten Thread."""
        config = self.config

        if not os.path.exists(config.excel_path):
            self._emit(ProgressEvent("error", f"Excel-Datei nicht gefunden: {config.excel_path}"))
            return

        if not config.api_key:
            self._emit(ProgressEvent("error", "Kein API-Key konfiguriert. Bitte .env prüfen."))
            return

        try:
            total = count_total_rows(config.excel_path)
        except Exception as e:
            self._emit(ProgressEvent("error", f"Excel konnte nicht gelesen werden: {e}"))
            return

        self._emit(ProgressEvent("log", f"Starte Batch-Verarbeitung: {total} ISINs gefunden"))

        processed = 0
        errors = 0

        for row_num, isin, fund_name, ms_seg in iter_unprocessed_isins(
            config.excel_path, skip_done=config.skip_done
        ):
            if self._stop_event.is_set():
                self._emit(ProgressEvent("log", "⏸ Verarbeitung angehalten."))
                break

            if processed >= config.batch_size:
                self._emit(ProgressEvent("log", f"Batch-Limit erreicht ({config.batch_size} ISINs)."))
                break

            processed += 1
            self._emit(ProgressEvent(
                "progress", f"Verarbeite {isin} ({fund_name[:30]}...)",
                isin=isin, current=processed, total=min(config.batch_size, total)
            ))

            pdf_path = None
            pdf_filename = ""

            # 1. PDF herunterladen
            try:
                self._emit(ProgressEvent("log", f"  📥 Lade PDF für {isin}..."))
                pdf_path = fetch_prospectus(
                    isin, fund_name, config.pdf_folder, delay=config.request_delay
                )
                if pdf_path:
                    pdf_filename = Path(pdf_path).name
                    self._emit(ProgressEvent("log", f"  ✅ PDF: {pdf_filename}"))
                else:
                    self._emit(ProgressEvent("log", f"  ⚠️  Kein PDF auf fundinfo.com gefunden"))
            except Exception as e:
                self._emit(ProgressEvent("log", f"  ❌ PDF-Download fehlgeschlagen: {e}"))
                logger.error(f"PDF-Download Fehler für {isin}: {e}")

            # 2. Text extrahieren und klassifizieren
            result = None
            web_context = None

            if pdf_path:
                try:
                    text = extract_relevant_text(pdf_path)
                    if text:
                        result = classify_prospectus(
                            text, isin=isin, fund_name=fund_name, api_key=config.api_key
                        )

                        # Web-Suche bei niedriger Konfidenz
                        if result.get("konfidenz") == "niedrig":
                            self._emit(ProgressEvent("log", f"  🔍 Konfidenz niedrig, Web-Suche..."))
                            web_context = search_fund_info(isin, fund_name)
                            if web_context:
                                result = classify_prospectus(
                                    text, isin=isin, fund_name=fund_name,
                                    additional_context=web_context, api_key=config.api_key
                                )
                except Exception as e:
                    logger.error(f"Klassifizierung fehlgeschlagen für {isin}: {e}")
                    result = None

            # 3. Fallback: Web-Suche ohne PDF
            if result is None:
                try:
                    self._emit(ProgressEvent("log", f"  🔍 Fallback: Web-Suche..."))
                    web_context = search_fund_info(isin, fund_name)
                    if web_context:
                        result = classify_prospectus(
                            web_context, isin=isin, fund_name=fund_name, api_key=config.api_key
                        )
                except Exception as e:
                    logger.error(f"Web-Fallback fehlgeschlagen für {isin}: {e}")

            # 4. Ergebnis in Excel schreiben
            if result:
                seg = result.get("segmentierung", "unklar")
                konfidenz = result.get("konfidenz", "")
                self._emit(ProgressEvent(
                    "result",
                    f"  ✅ {isin} → {seg} ({konfidenz})",
                    isin=isin, result=result
                ))
                try:
                    write_result(config.excel_path, row_num, result, pdf_filename)
                except Exception as e:
                    logger.error(f"Schreiben fehlgeschlagen für {isin}: {e}")
                    errors += 1
            else:
                errors += 1
                self._emit(ProgressEvent("log", f"  ❌ Konnte {isin} nicht klassifizieren"))
                try:
                    write_error(config.excel_path, row_num, "Klassifizierung fehlgeschlagen")
                except Exception as e:
                    logger.error(f"Fehler-Status schreiben fehlgeschlagen für {isin}: {e}")

        self._emit(ProgressEvent(
            "done",
            f"Fertig! {processed} verarbeitet, {errors} Fehler.",
            current=processed, total=processed
        ))
