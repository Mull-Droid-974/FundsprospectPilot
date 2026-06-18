"""
Direkte LLM-Analyse für PDFs <= 150 Seiten ohne Extraktion.
Kein Trim, kein Extract — der Analyse-Worker liest Rohtext direkt.
Starten wenn API-Budget verfügbar (reset 2026-07-01).
"""
import os
import queue
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from dotenv import load_dotenv
load_dotenv()

import pdf_analyzer
import results_store
import typologie_store
from llm_analysis_worker import AnalysisEvent, LLMAnalysisWorker
from prospekt_analysis_window import DEFAULT_PROMPT

API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL   = "claude-haiku-4-5-20251001"
_t0     = time.time()


def _log(msg: str):
    e = int(time.time() - _t0)
    h, r = divmod(e, 3600); m, s = divmod(r, 60)
    print(f"[{h:02d}:{m:02d}:{s:02d}] {msg.encode('ascii', 'replace').decode()}", flush=True)


def build_prompt() -> str:
    ft = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
    at = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
    kt = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
    return (DEFAULT_PROMPT
            .replace("{fondstyp_liste}", ft)
            .replace("{anlegertyp_liste}", at)
            .replace("{kundentyp_liste}", kt))


def find_groups() -> dict[str, list]:
    """Alle ausstehenden Gruppen deren PDF <= 150 Seiten hat und kein pre-processing braucht."""
    results_store.init_db()
    all_rows = results_store.get_all_results()

    groups: dict[str, list] = {}
    seen_pdfs: dict[str, bool] = {}  # pdf_path -> passt

    for row in all_rows:
        if row.get("llm_segmentierung"):
            continue  # bereits analysiert

        pdf_path = row.get("prospekt_pfad") or ""
        if not pdf_path or not Path(pdf_path).exists():
            continue

        # PDF-Eignung cachen
        if pdf_path not in seen_pdfs:
            pdf = Path(pdf_path)
            has_ext  = pdf.with_suffix(".extracted.json").exists()
            has_trim = pdf.with_suffix(".trimmed.txt").exists()
            if has_ext or has_trim:
                # Hat schon pre-processing — trotzdem OK, wird bevorzugt
                seen_pdfs[pdf_path] = True
            else:
                mb = pdf.stat().st_size / 1_048_576
                if mb > 5.0:
                    seen_pdfs[pdf_path] = False
                else:
                    meta = pdf_analyzer.get_pdf_metadata(pdf_path)
                    pages = meta.get("pages", 999)
                    seen_pdfs[pdf_path] = (pages <= 150)

        if not seen_pdfs[pdf_path]:
            continue

        key = row.get("subfonds_id") or f"__single_{row['isin']}"
        groups.setdefault(key, []).append(row)

    return groups


if __name__ == "__main__":
    if not API_KEY:
        print("ANTHROPIC_API_KEY fehlt"); sys.exit(1)

    _log("Lade ausstehende Gruppen (PDF <= 150 Seiten) ...")
    groups = find_groups()
    n_isins = sum(len(v) for v in groups.values())
    _log(f"Gruppen: {len(groups)} | ISINs: {n_isins}")

    if not groups:
        _log("Nichts zu tun."); sys.exit(0)

    prompt   = build_prompt()
    ev_queue = queue.Queue()

    worker = LLMAnalysisWorker(
        groups=groups,
        prompt_template=prompt,
        model=MODEL,
        api_key=API_KEY,
        event_queue=ev_queue,
        delay=0.5,
        workers=2,
        provider="anthropic",
    )
    worker.start()
    _log(f"Worker gestartet (2 parallel, Modell: {MODEL}) ...")

    done = skipped = failed = 0
    while True:
        try:
            evt: AnalysisEvent = ev_queue.get(timeout=300)
            if evt.type == "progress":
                done    = evt.done
                skipped = evt.skipped
                failed  = evt.failed
                pct = done * 100 // max(len(groups), 1)
                _log(f"  OK [{pct:3d}%] {done}/{len(groups)} | skip={skipped} fail={failed} | {evt.message.encode('ascii','replace').decode()}")
            elif evt.type == "error":
                _log(f"  ERR: {evt.isin} — {evt.message.encode('ascii','replace').decode()}")
            elif evt.type == "log":
                _log(f"  LOG: {evt.message.encode('ascii','replace').decode()}")
            elif evt.type == "done":
                _log(f"FERTIG: {evt.message.encode('ascii','replace').decode()}")
                break
        except queue.Empty:
            if not worker.is_alive():
                _log("Worker beendet."); break
            _log("  ... warte ...")

    worker.join(timeout=10)
    _log("=== ABGESCHLOSSEN ===")
