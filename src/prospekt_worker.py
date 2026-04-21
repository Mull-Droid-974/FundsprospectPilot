"""
Prospekt-Download-Worker: Lädt Verkaufsprospekte für ISINs von fundinfo.com herunter.

Pipeline pro ISIN:
  1. Skip wenn prospekt_pfad gesetzt UND Datei auf Disk vorhanden (idempotent)
  2. fetch_prospectus() via fundinfo API
  3. DB-Update (prospekt_pfad + prospekt_url)
"""

import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import fundinfo_client
import results_store


@dataclass
class ProspektEvent:
    type: str       # "log" | "progress" | "done" | "error"
    isin: str = ""
    message: str = ""
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0


class ProspektWorker(threading.Thread):
    def __init__(
        self,
        isins: list[dict],
        pdf_folder: Path,
        event_queue: queue.Queue,
        delay: float = 1.5,
    ):
        super().__init__(daemon=True)
        self._isins = isins
        self._pdf_folder = pdf_folder
        self._queue = event_queue
        self._delay = delay
        self._stop_flag = False
        self._done = 0
        self._skipped = 0
        self._failed = 0

    def stop(self):
        self._stop_flag = True

    def _emit(self, type_: str, isin: str = "", message: str = ""):
        self._queue.put(ProspektEvent(
            type=type_,
            isin=isin,
            message=message,
            total=len(self._isins),
            done=self._done,
            skipped=self._skipped,
            failed=self._failed,
        ))

    def run(self):
        self._pdf_folder.mkdir(parents=True, exist_ok=True)

        for row in self._isins:
            if self._stop_flag:
                self._emit("log", message="Download abgebrochen.")
                break

            isin = row.get("isin", "")
            fondsname = row.get("fondsname", "")
            existing = row.get("prospekt_pfad", "")

            # Idempotenz-Check
            if existing and Path(existing).exists():
                self._skipped += 1
                self._emit("log", isin, f"Übersprungen (bereits vorhanden): {Path(existing).name}")
                continue

            self._emit("log", isin, f"Suche Prospekt-URL für {isin} ({fondsname}) …")

            try:
                doc_info = fundinfo_client.discover_prospectus_url(isin, delay=self._delay)
            except Exception as exc:
                self._failed += 1
                self._emit("error", isin, f"Fehler bei URL-Suche: {exc}")
                continue

            if not doc_info:
                self._failed += 1
                self._emit("error", isin, "Kein Prospekt gefunden (alle Profile versucht)")
                continue

            # Duplikat-Check: URL bereits von anderer ISIN heruntergeladen?
            existing_row = results_store.get_by_prospekt_url(doc_info["url"])
            if existing_row and Path(existing_row["prospekt_pfad"]).exists():
                results_store.update_prospekt(isin, existing_row["prospekt_pfad"], doc_info["url"])
                self._done += 1
                self._emit(
                    "progress", isin,
                    f"Verknüpft (gleicher Prospekt wie {existing_row['isin']}): "
                    f"{Path(existing_row['prospekt_pfad']).name}"
                )
                continue

            # Neu herunterladen
            try:
                result = fundinfo_client.fetch_prospectus(isin, fondsname, str(self._pdf_folder))
            except Exception as exc:
                self._failed += 1
                self._emit("error", isin, f"Download-Fehler: {exc}")
                continue

            if result and result.pdf_path:
                results_store.update_prospekt(isin, result.pdf_path, result.pdf_url)
                self._done += 1
                self._emit("progress", isin, f"Gespeichert: {Path(result.pdf_path).name}")
            else:
                self._failed += 1
                self._emit("error", isin, "Download fehlgeschlagen")

            time.sleep(self._delay)

        self._emit("done", message=f"Fertig. Neu: {self._done}, Übersprungen: {self._skipped}, Fehler: {self._failed}")
