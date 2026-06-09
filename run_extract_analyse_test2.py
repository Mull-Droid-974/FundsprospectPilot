"""
Test 2: Zwei grosse PDFs extrahieren, analysieren und Ergebnisse plausibilisieren.
PDFs: Robeco High Yield Bonds (26.5 MB, 1061 S.) + BNP AQUA (20 MB, 1406 S.)
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

import results_store
import typologie_store
import pdf_analyzer
from llm_analysis_worker import AnalysisEvent, LLMAnalysisWorker
from pdf_trim_window import _ExtractWorker
from prospekt_analysis_window import DEFAULT_PROMPT

API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_t0 = time.time()

TEST_PDFS = [
    Path("data/prospekte/Robeco_All_Strategy_Euro_Bonds_XX.pdf"),
    Path(r"data/prospekte/HSBC_Global_Investment_Funds_-_Global_High_Yi_7f90bbba_XX.pdf"),
]


def _log(msg: str):
    e = int(time.time() - _t0)
    h, r = divmod(e, 3600); m, s = divmod(r, 60)
    print(f"[{h:02d}:{m:02d}:{s:02d}] {msg.encode('ascii','replace').decode()}", flush=True)


# ─── Schritt 1: Extraktion ────────────────────────────────────────────────────

def extract_pdf(pdf: Path) -> dict:
    _log(f"\n{'='*60}")
    _log(f"EXTRAHIERE: {pdf.name}  ({pdf.stat().st_size/1_048_576:.1f} MB)")

    ext = pdf.with_suffix(".extracted.json")
    if ext.exists():
        _log("  Vorhandene .extracted.json geloescht (Neustart).")
        ext.unlink()

    ev: queue.Queue = queue.Queue()
    worker = _ExtractWorker(str(pdf), "claude-haiku-4-5-20251001", API_KEY, ev)
    worker.start()

    extracted = {}
    while True:
        try:
            t, p = ev.get(timeout=600)
            if t == "log":   _log(f"  {p}")
            elif t == "error": _log(f"  FEHLER: {p}")
            elif t == "extracted": extracted = p
            elif t == "done": break
        except queue.Empty:
            _log("  TIMEOUT"); break
    worker.join(timeout=10)
    return extracted


# ─── Schritt 2: Analyse ───────────────────────────────────────────────────────

def analyse_pdf(pdf: Path):
    _log(f"\n  --- ANALYSE: {pdf.name} ---")
    results_store.init_db()
    all_rows = results_store.get_all_results()

    groups: dict[str, list] = {}
    for row in all_rows:
        if Path(row.get("prospekt_pfad", "")).name != pdf.name:
            continue
        key = row.get("subfonds_id") or f"__single_{row['isin']}"
        groups.setdefault(key, []).append(row)

    if not groups:
        _log("  Keine ISINs in DB!"); return

    # LLM-Ergebnisse zuruecksetzen
    all_isins = [r["isin"] for rows in groups.values() for r in rows]
    results_store.reset_llm_analysis(all_isins)
    _log(f"  {len(all_isins)} ISINs zurueckgesetzt | {len(groups)} Gruppe(n)")

    ft = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
    at = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
    kt = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
    prompt = (DEFAULT_PROMPT
              .replace("{fondstyp_liste}", ft)
              .replace("{anlegertyp_liste}", at)
              .replace("{kundentyp_liste}", kt))

    ev: queue.Queue = queue.Queue()
    worker = LLMAnalysisWorker(
        groups=groups, prompt_template=prompt,
        model="claude-sonnet-4-6", api_key=API_KEY,
        event_queue=ev, delay=0.5, workers=1, provider="anthropic",
    )
    worker.start()

    while True:
        try:
            evt: AnalysisEvent = ev.get(timeout=300)
            prefix = {"log":"LOG","progress":"OK ","error":"ERR","done":"DONE"}.get(evt.type,"   ")
            _log(f"  {prefix}: {evt.message.encode('ascii','replace').decode()}")
            if evt.type == "done": break
        except queue.Empty:
            _log("  TIMEOUT"); break
    worker.join(timeout=10)


# ─── Schritt 3: Plausibilitaet ───────────────────────────────────────────────

def plausibilisiere(pdf: Path):
    _log(f"\n  --- PLAUSIBILISIERUNG: {pdf.name} ---")

    # extracted.json pruefen
    ext = pdf.with_suffix(".extracted.json")
    if not ext.exists():
        _log("  FEHLER: .extracted.json fehlt!"); return

    data = json.loads(ext.read_text(encoding="utf-8"))
    umbrella = data.get("umbrella") or {}
    portfolios = data.get("portfolios") or []

    _log(f"  Umbrella: {umbrella.get('name','?')}")
    _log(f"  Regulierung: {umbrella.get('regulierung','?')}")
    _log(f"  Portfolios: {len(portfolios)}")

    n_kl = n_isin = n_min = n_anleger = 0
    for pf in portfolios:
        klassen = pf.get("anteilsklassen") or []
        n_kl += len(klassen)
        for kl in klassen:
            if kl.get("isin"):                     n_isin    += 1
            if kl.get("mindestanlage"):            n_min     += 1
            if kl.get("anlegertyp_beschraenkung"): n_anleger += 1
        _log(f"    '{(pf.get('name') or '?')[:55]}': {len(klassen)} Klassen | "
             f"fondstyp={pf.get('fondstyp','?')}")

    _log(f"  Anteilsklassen total:  {n_kl}")
    _log(f"    mit ISIN:            {n_isin}/{n_kl}  ({n_isin*100//max(n_kl,1)}%)")
    _log(f"    mit Mindestanlage:   {n_min}/{n_kl}  ({n_min*100//max(n_kl,1)}%)")
    _log(f"    mit Anlegertyp:      {n_anleger}/{n_kl}  ({n_anleger*100//max(n_kl,1)}%)")

    # DB-Ergebnisse
    con = sqlite3.connect("data/output/results.db")
    con.row_factory = sqlite3.Row
    db = con.execute(
        "SELECT isin, llm_segmentierung, fondstyp, anlegertyp, mindestanlage, begruendung "
        "FROM fund_results WHERE prospekt_pfad = ?",
        (str(pdf.resolve()),)
    ).fetchall()

    n_total  = len(db)
    n_ok     = sum(1 for r in db if r["llm_segmentierung"] not in ("", "unklar", None))
    n_unklar = sum(1 for r in db if r["llm_segmentierung"] == "unklar")
    n_leer   = sum(1 for r in db if not r["llm_segmentierung"])
    segs: dict[str, int] = {}
    for r in db:
        s = r["llm_segmentierung"] or "(leer)"
        segs[s] = segs.get(s, 0) + 1

    _log(f"\n  DB: {n_total} ISINs | klassifiziert={n_ok} | unklar={n_unklar} | leer={n_leer}")
    _log(f"  Segmentierungen: {dict(sorted(segs.items(), key=lambda x:-x[1]))}")

    # Stichprobe
    _log("  Stichprobe (alle ISINs):")
    for r in db:
        seg = r["llm_segmentierung"] or "?"
        ft  = (r["fondstyp"] or "?")[:22]
        at  = (r["anlegertyp"] or "")[:20]
        ma  = (r["mindestanlage"] or "")[:15]
        beg = (r["begruendung"] or "")[:55]
        _log(f"    {r['isin']} | seg={seg:15} | {ft:22} | min={ma:15} | {beg}")

    # Plausibilitaets-Check
    _log("\n  CHECKS:")
    issues = []

    if n_kl == 0:
        issues.append("KRITISCH: Keine Anteilsklassen extrahiert")
    if n_isin == 0:
        issues.append("WARNUNG: Keine ISINs im extracted.json (ISINs nicht im PDF-Text)")
    if n_ok == 0:
        issues.append("KRITISCH: Kein einziger ISIN klassifiziert")
    if n_unklar == n_total and n_total > 0:
        issues.append("WARNUNG: Alle ISINs 'unklar' - extracted.json reicht nicht fuer Klassifizierung")

    # Konsistenz: fondstyp sollte einheitlich sein
    fondstypen = set(r["fondstyp"] for r in db if r["fondstyp"])
    if len(fondstypen) > 3:
        issues.append(f"WARNUNG: {len(fondstypen)} verschiedene Fondstypen - ungewoehnlich")

    # Mindestanlage check
    retail_ohne_mindest = [r for r in db
                           if r["llm_segmentierung"] == "retail" and not r["mindestanlage"]]
    if retail_ohne_mindest:
        issues.append(f"INFO: {len(retail_ohne_mindest)} retail-ISINs ohne Mindestanlage (normal fuer UCITS)")

    if not issues:
        _log("  ALLE CHECKS OK - Ergebnisse plausibel")
    else:
        for issue in issues:
            _log(f"  {issue}")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not API_KEY:
        print("ANTHROPIC_API_KEY fehlt"); sys.exit(1)

    results_store.init_db()

    for pdf in TEST_PDFS:
        if not pdf.exists():
            _log(f"PDF nicht gefunden: {pdf}"); continue

        extract_pdf(pdf)
        analyse_pdf(pdf)
        plausibilisiere(pdf)

    _log("\n=== ABGESCHLOSSEN ===")
