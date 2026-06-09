"""
Reparatur-Download: lädt korrekte PDFs für alle ISINs ohne prospekt_pfad.
Nutzt denselben ProspektWorker wie der GUI-Button "Alle fehlenden downloaden".
"""

import os
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from dotenv import load_dotenv
load_dotenv()

import results_store
from prospekt_worker import ProspektEvent, ProspektWorker

PDF_FOLDER = Path(__file__).parent / "data" / "prospekte"

_t0 = time.time()


def _log(msg: str):
    elapsed = int(time.time() - _t0)
    h, r = divmod(elapsed, 3600)
    m, s = divmod(r, 60)
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{h:02d}:{m:02d}:{s:02d}] {safe}", flush=True)


if __name__ == "__main__":
    results_store.init_db()

    queue_rows = results_store.get_prospekt_queue(skip_nicht_gefunden=True)
    _log(f"ISINs ohne PDF (exkl. nicht_gefunden): {len(queue_rows)}")

    if not queue_rows:
        _log("Nichts zu tun.")
        sys.exit(0)

    ev_queue: queue.Queue = queue.Queue()
    worker = ProspektWorker(
        isins=queue_rows,
        pdf_folder=PDF_FOLDER,
        event_queue=ev_queue,
        delay=1.5,
        parallel=3,
        meta_parallel=4,
    )
    worker.start()
    _log("Worker gestartet ...")

    done = skipped = failed = 0
    total = len(queue_rows)
    last_progress = time.time()

    while True:
        try:
            evt: ProspektEvent = ev_queue.get(timeout=300)

            if evt.type == "log":
                _log(f"  {evt.message}")
            elif evt.type == "progress":
                done    = evt.done
                skipped = evt.skipped
                failed  = evt.failed
                pct = done * 100 // max(total, 1)
                _log(f"  OK [{pct:3d}%] {done}/{total} | skip={skipped} fail={failed} | {evt.message}")
                last_progress = time.time()
            elif evt.type == "error":
                _log(f"  ERR {evt.isin}: {evt.message}")
            elif evt.type == "done":
                _log(f"FERTIG: {evt.message}")
                break

        except queue.Empty:
            if not worker.is_alive():
                _log("Worker beendet (kein DONE-Event).")
                break
            _log(f"  ... warte (letzte Aktivitaet vor {int(time.time()-last_progress)}s)")

    worker.join(timeout=10)

    # Abschluss-Audit
    wrong = results_store.get_wrong_prospekt_links()
    n_wrong = sum(len(v) for v in wrong.values())
    _log(f"Audit nach Repair: {n_wrong} falsch verknuepfte ISINs verbleibend")

    remaining = results_store.get_prospekt_queue(skip_nicht_gefunden=True)
    _log(f"Noch ohne PDF (exkl. nicht_gefunden): {len(remaining)}")
    _log("=== Repair abgeschlossen ===")
