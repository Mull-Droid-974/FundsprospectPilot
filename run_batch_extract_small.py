"""
Batch-Extraktion für kleine PDFs (< 50 Seiten = single chunk, kein Rate-Limit zwischen Chunks).
Zwischen PDFs ~45s Pause um Token-Budget einzuhalten (50K Tokens/Minute).
Startet automatisch die LLM-Analyse nach jeder Extraktion.
"""
import json
import os
import queue
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from dotenv import load_dotenv
load_dotenv()

import pdfplumber
import results_store
import typologie_store
from llm_analysis_worker import AnalysisEvent, LLMAnalysisWorker
from pdf_trim_window import _ExtractWorker
from prospekt_analysis_window import DEFAULT_PROMPT

API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
MAX_PAGES = 50          # Grenze fuer single-chunk
INTER_PDF_MIN = 45.0    # Mindest-Pause zwischen PDFs (Sekunden)
_t0 = time.time()


def _log(msg: str):
    e = int(time.time() - _t0)
    h, r = divmod(e, 3600); m, s = divmod(r, 60)
    print(f"[{h:02d}:{m:02d}:{s:02d}] {msg.encode('ascii', 'replace').decode()}", flush=True)


def _build_prompt() -> str:
    ft = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
    at = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
    kt = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
    return (DEFAULT_PROMPT
            .replace("{fondstyp_liste}", ft)
            .replace("{anlegertyp_liste}", at)
            .replace("{kundentyp_liste}", kt))


def find_candidates() -> list[Path]:
    con = sqlite3.connect("data/output/results.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT DISTINCT prospekt_pfad FROM fund_results "
        "WHERE prospekt_pfad IS NOT NULL AND prospekt_pfad != ''"
    ).fetchall()

    candidates = []
    for r in rows:
        pdf = Path(r["prospekt_pfad"])
        if not pdf.exists(): continue
        if pdf.with_suffix(".extracted.json").exists(): continue
        mb = pdf.stat().st_size / 1_048_576
        if mb < 0.05 or mb > 2.0: continue   # Grössen-Vorfilter
        try:
            with pdfplumber.open(str(pdf)) as p:
                pages = len(p.pages)
        except:
            continue
        if pages >= MAX_PAGES: continue
        candidates.append(pdf)

    candidates.sort(key=lambda p: p.stat().st_size)
    return candidates


def extract_one(pdf: Path) -> bool:
    ev: queue.Queue = queue.Queue()
    worker = _ExtractWorker(str(pdf), "claude-haiku-4-5-20251001", API_KEY, ev)
    worker.start()
    success = False
    while True:
        try:
            t, p = ev.get(timeout=120)
            if t == "error":   _log(f"    ERR: {p}")
            elif t == "extracted" and p: success = True
            elif t == "done":  break
        except queue.Empty:
            _log("    TIMEOUT"); break
    worker.join(timeout=5)
    return success


def analyse_pdf(pdf: Path, prompt: str):
    con = sqlite3.connect("data/output/results.db")
    con.row_factory = sqlite3.Row
    all_rows = [dict(r) for r in con.execute(
        "SELECT * FROM fund_results WHERE prospekt_pfad = ?", (str(pdf),)
    ).fetchall()]

    if not all_rows:
        return

    # Nur ISINs ohne bestehende LLM-Segmentierung
    pending = [r for r in all_rows if not r.get("llm_segmentierung")]
    if not pending:
        _log("    Alle ISINs bereits analysiert — uebersprungen.")
        return

    groups: dict[str, list] = {}
    for row in pending:
        key = row.get("subfonds_id") or f"__single_{row['isin']}"
        groups.setdefault(key, []).append(row)

    ev: queue.Queue = queue.Queue()
    worker = LLMAnalysisWorker(
        groups=groups, prompt_template=prompt,
        model="claude-sonnet-4-6", api_key=API_KEY,
        event_queue=ev, delay=0.3, workers=1, provider="anthropic",
    )
    worker.start()
    while True:
        try:
            evt: AnalysisEvent = ev.get(timeout=120)
            if evt.type in ("progress", "done"):
                _log(f"    {evt.type.upper()}: {evt.message.encode('ascii','replace').decode()}")
            elif evt.type == "error":
                _log(f"    ERR: {evt.message.encode('ascii','replace').decode()}")
            if evt.type == "done": break
        except queue.Empty:
            _log("    ANALYSE TIMEOUT"); break
    worker.join(timeout=5)


if __name__ == "__main__":
    if not API_KEY:
        print("ANTHROPIC_API_KEY fehlt"); sys.exit(1)

    results_store.init_db()
    prompt = _build_prompt()

    _log("Suche kleine PDFs (< 50 Seiten, kein extracted.json) ...")
    candidates = find_candidates()
    _log(f"Gefunden: {len(candidates)} PDFs")

    for i, pdf in enumerate(candidates, 1):
        mb = pdf.stat().st_size / 1_048_576
        _log(f"\n[{i}/{len(candidates)}] {pdf.name}  ({mb:.1f} MB)")

        t_start = time.time()

        # Extraktion
        ok = extract_one(pdf)
        if not ok:
            _log("    Extraktion fehlgeschlagen — weiter.")
            continue

        ext = pdf.with_suffix(".extracted.json")
        if ext.exists():
            data = json.loads(ext.read_text(encoding="utf-8"))
            n_pf = len(data.get("portfolios") or [])
            n_kl = sum(len(pf.get("anteilsklassen") or []) for pf in (data.get("portfolios") or []))
            _log(f"    Extrahiert: {n_pf} Portfolios, {n_kl} Klassen")

        # Analyse
        analyse_pdf(pdf, prompt)

        # Inter-PDF-Pause (Rate-Limit)
        elapsed = time.time() - t_start
        wait = max(0.0, INTER_PDF_MIN - elapsed)
        if wait > 1 and i < len(candidates):
            _log(f"    Pause {wait:.0f}s ...")
            time.sleep(wait)

    _log(f"\n=== BATCH FERTIG: {len(candidates)} PDFs verarbeitet ===")
