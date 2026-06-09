"""
Extraktions-Test: zwei PDFs per _ExtractWorker verarbeiten, analysieren, Qualität prüfen.
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

import pdf_analyzer
import results_store
import typologie_store
from llm_analysis_worker import AnalysisEvent, LLMAnalysisWorker
from pdf_trim_window import _ExtractWorker, _EXTRACT_PROMPT, _merge_extracted

API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL_EXTRACT  = "claude-haiku-4-5-20251001"
MODEL_ANALYSE  = "claude-sonnet-4-6"

TEST_PDFS = [
    Path("data/prospekte/Swiss_Equity_XX.pdf"),
    Path("data/prospekte/Emerging_Markets_Equity_Fund_XX.pdf"),
]


def _log(msg: str):
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


# ─── Schritt 1: Extraktion ────────────────────────────────────────────────────

def extract_pdf(pdf_path: Path) -> dict:
    _log(f"\n{'='*60}")
    _log(f"EXTRAHIERE: {pdf_path.name}")
    _log(f"  Groesse: {pdf_path.stat().st_size/1_048_576:.1f} MB")

    # Alte extracted.json loeschen fuer sauberen Test
    ext_file = pdf_path.with_suffix(".extracted.json")
    if ext_file.exists():
        ext_file.unlink()
        _log("  Alte .extracted.json geloescht.")

    ev_queue: queue.Queue = queue.Queue()
    worker = _ExtractWorker(
        pdf_path=str(pdf_path),
        model=MODEL_EXTRACT,
        api_key=API_KEY,
        result_queue=ev_queue,
        provider="anthropic",
    )
    worker.start()

    extracted = {}
    while True:
        try:
            evt_type, payload = ev_queue.get(timeout=600)
            if evt_type == "log":
                _log(f"  {payload}")
            elif evt_type == "error":
                _log(f"  FEHLER: {payload}")
            elif evt_type == "extracted":
                extracted = payload
            elif evt_type == "done":
                break
        except queue.Empty:
            _log("  TIMEOUT beim Warten auf Worker")
            break

    worker.join(timeout=10)
    return extracted


# ─── Schritt 2: Analyse ───────────────────────────────────────────────────────

def analyse_pdf(pdf_path: Path):
    _log(f"\n  -- Analyse: {pdf_path.name} --")
    results_store.init_db()
    all_rows = results_store.get_all_results()

    groups: dict[str, list] = {}
    for row in all_rows:
        pfad = row.get("prospekt_pfad") or ""
        if Path(pfad).name != pdf_path.name:
            continue
        key = row.get("subfonds_id") or f"__single_{row['isin']}"
        groups.setdefault(key, []).append(row)

    if not groups:
        _log("  Keine ISINs in DB fuer dieses PDF gefunden.")
        return

    # Reset bestehende LLM-Ergebnisse fuer sauberen Test
    all_isins = [r["isin"] for rows in groups.values() for r in rows]
    results_store.reset_llm_analysis(all_isins)
    _log(f"  {len(all_isins)} ISINs zurueckgesetzt. {len(groups)} Gruppe(n).")

    # Prompt mit Taxonomie
    fondstyp_liste   = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
    anlegertyp_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
    kundentyp_liste  = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))

    from prospekt_analysis_window import DEFAULT_PROMPT
    prompt = (DEFAULT_PROMPT
              .replace("{fondstyp_liste}",   fondstyp_liste)
              .replace("{anlegertyp_liste}",  anlegertyp_liste)
              .replace("{kundentyp_liste}",  kundentyp_liste))

    ev_queue: queue.Queue = queue.Queue()
    worker = LLMAnalysisWorker(
        groups=groups,
        prompt_template=prompt,
        model=MODEL_ANALYSE,
        api_key=API_KEY,
        event_queue=ev_queue,
        delay=0.5,
        workers=1,
        provider="anthropic",
    )
    worker.start()

    while True:
        try:
            evt: AnalysisEvent = ev_queue.get(timeout=300)
            prefix = {"log": "LOG", "progress": "OK ", "error": "ERR", "done": "DONE"}.get(evt.type, "   ")
            _log(f"  {prefix}: {evt.message}")
            if evt.type == "done":
                break
        except queue.Empty:
            _log("  TIMEOUT")
            break

    worker.join(timeout=10)


# ─── Schritt 3: Qualitaetscheck ───────────────────────────────────────────────

def check_quality(pdf_path: Path):
    _log(f"\n  -- Qualitaet: {pdf_path.name} --")

    # extracted.json pruefen
    ext_file = pdf_path.with_suffix(".extracted.json")
    if not ext_file.exists():
        _log("  FEHLER: .extracted.json nicht vorhanden!")
        return

    data = json.loads(ext_file.read_text(encoding="utf-8"))
    umbrella = data.get("umbrella") or {}
    portfolios = data.get("portfolios") or []

    _log(f"  Umbrella: {umbrella.get('name','?')} | Regulierung: {umbrella.get('regulierung','?')}")
    _log(f"  Portfolios: {len(portfolios)}")

    n_klassen = 0
    n_mit_isin = 0
    n_mit_mindest = 0
    n_mit_anleger = 0
    for pf in portfolios:
        klassen = pf.get("anteilsklassen") or []
        n_klassen += len(klassen)
        for kl in klassen:
            if kl.get("isin"):              n_mit_isin    += 1
            if kl.get("mindestanlage"):     n_mit_mindest += 1
            if kl.get("anlegertyp_beschraenkung"): n_mit_anleger += 1
        _log(f"    Portfolio '{(pf.get('name') or '?')[:50]}': "
             f"{len(klassen)} Klassen | fondstyp={pf.get('fondstyp','?')}")

    _log(f"  Anteilsklassen gesamt: {n_klassen}")
    _log(f"    mit ISIN:        {n_mit_isin}/{n_klassen} ({n_mit_isin*100//max(n_klassen,1)}%)")
    _log(f"    mit Mindestanl.: {n_mit_mindest}/{n_klassen} ({n_mit_mindest*100//max(n_klassen,1)}%)")
    _log(f"    mit Anlegertyp:  {n_mit_anleger}/{n_klassen} ({n_mit_anleger*100//max(n_klassen,1)}%)")

    # DB-Ergebnisse pruefen
    con = sqlite3.connect("data/output/results.db")
    con.row_factory = sqlite3.Row
    db_rows = con.execute(
        "SELECT isin, llm_segmentierung, fondstyp, anlegertyp, mindestanlage "
        "FROM fund_results WHERE prospekt_pfad LIKE ?",
        (f"%{pdf_path.name}%",)
    ).fetchall()

    n_total   = len(db_rows)
    n_seg_ok  = sum(1 for r in db_rows if r["llm_segmentierung"] and r["llm_segmentierung"] != "unklar")
    n_unklar  = sum(1 for r in db_rows if r["llm_segmentierung"] == "unklar")
    n_leer    = sum(1 for r in db_rows if not r["llm_segmentierung"])
    segs      = {}
    for r in db_rows:
        s = r["llm_segmentierung"] or "(leer)"
        segs[s] = segs.get(s, 0) + 1

    _log(f"\n  DB-ISINs: {n_total} | klassifiziert: {n_seg_ok} | unklar: {n_unklar} | leer: {n_leer}")
    _log(f"  Segmentierungen: {dict(sorted(segs.items(), key=lambda x:-x[1]))}")

    # Stichprobe: erste 5 ISINs
    _log("  Stichprobe (5 ISINs):")
    for r in db_rows[:5]:
        _log(f"    {r['isin']} | seg={r['llm_segmentierung'] or '?':15} "
             f"| ft={r['fondstyp'] or '?':20} | min={r['mindestanlage'] or '':12}")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not API_KEY:
        print("ANTHROPIC_API_KEY fehlt in .env")
        sys.exit(1)

    results_store.init_db()

    for pdf in TEST_PDFS:
        if not pdf.exists():
            _log(f"PDF nicht gefunden: {pdf}")
            continue

        # 1. Extrahieren
        extract_pdf(pdf)

        # 2. Analysieren
        analyse_pdf(pdf)

        # 3. Qualitaet pruefen
        check_quality(pdf)

    _log("\n=== TEST ABGESCHLOSSEN ===")
