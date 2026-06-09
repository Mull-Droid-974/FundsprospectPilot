"""
Einmaliges Analyse-Skript: Goldman Sachs Emerging Markets PDF trimmen + analysieren.

Ablauf:
  1. PDF (18 MB) per LLM kürzen → .trimmed.txt speichern
  2. Alle 3 Subfonds-Gruppen (55 ISINs) per LLM klassifizieren
  3. Ergebnisse in results.db schreiben
"""

import os
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import anthropic
from dotenv import load_dotenv

load_dotenv()

import pdf_analyzer
import results_store
import typologie_store
from llm_analysis_worker import AnalysisEvent, LLMAnalysisWorker

PDF_PATH = Path(__file__).parent / "data" / "prospekte" / "Goldman_Sachs_Emerging_Markets_XX.pdf"

TRIM_PROMPT = """\
Kürze den folgenden Prospekt-Text auf die für die Anteilsklassen-Klassifizierung \
relevanten Abschnitte.

BEHALTEN (vollständig):
- Abschnitts-Überschriften und Fondsnamen (Umbrella-Fonds, Subfonds) als struktureller Kontext
- Anteilsklassen-Tabellen mit ISIN, Mindestanlage, Währung, TER
- Anleger-Beschränkungen und Zulassungskriterien
- Regulatorische Klassifizierungen (MiFID, KAG/FIDLEG, UCITS, AIFMD)
- Vertriebsbeschränkungen nach Land / Investorentyp
- Abschnitte zu "Qualified Investors", "Professional Investors", "Retail"

ENTFERNEN (weglassen):
- Allgemeine Risikobeschreibungen ohne Bezug zu Anlegertypen
- Verwaltungsdetails (Depotbank, Revisoren, Zeichnungsfristen ohne Mindestanlage)
- Historische Performance-Daten
- Allgemeine Steuerhinweise

Gib NUR den gekürzten Text zurück — kein Kommentar, keine Erklärung, \
keine Markdown-Formatierung.\
"""

ANALYSIS_PROMPT = """\
Du bist ein erfahrener Wertpapierrechtsspezialist mit umfassenden Kenntnissen des \
Schweizer Kollektivanlagerechts (KAG/CISA, FIDLEG), UCITS/OGAW-Richtlinien, AIFMD \
sowie MiFID II Anlegerklassifizierung.

Analysiere den nachfolgenden Verkaufsprospekt-Auszug und bestimme die nachfolgenden \
Felder. Wichtig: Verwende für FONDSTYP, ANLEGERTYP und KUNDENTYP AUSSCHLIESSLICH \
die erlaubten Werte aus den Listen — wähle den semantisch nächstliegenden Wert. \
ANLEGERTYP und KUNDENTYP werden pro Anteilsklasse bestimmt (nicht pro Subfonds).

=== ERLAUBTE WERTE (zwingend auf diese mappen) ===

FONDSTYP:
{fondstyp_liste}

ANLEGERTYP:
{anlegertyp_liste}

KUNDENTYP (primärer, wichtigster Kundentyp der Anteilsklasse):
{kundentyp_liste}

=== INFORMATIONSHIERARCHIE IM PROSPEKT ===

Fondsprospekte sind hierarchisch aufgebaut:
1. UMBRELLA-EBENE: Allgemeine Dachfonds-Informationen (Verwaltungsgesellschaft, Rechtsform, ...)
2. SUBFONDS-EBENE: Anlageziel, Benchmark, Risikoklasse — gilt für alle Anteilsklassen
3. ANTEILSKLASSEN-EBENE: Klassenspezifische Details (Mindestanlage, TER, Vertriebsbeschränkungen)

NICHT verwenden als anteilsklassen-spezifische Information:
- Allgemeine Abschnitte "Anteilsklassenkonzept" oder "Share Class Framework" \
(beschreiben mögliche Merkmale, keine konkreten Klassen-Parameter)
- Umbrella-weite Aussagen über Mindestzeiträume oder Mindestgrössen
- Formulierungen wie "der Fonds kann Klassen für..." oder "es sind Klassen möglich..."

=== AUFGABE 1 — Fondstyp (Subfonds-Ebene) ===

Bestimme für den gesamten Subfonds:
- FONDSTYP: exakt ein Wert aus obiger Liste
- fondstyp_roh: exakte Formulierung aus dem Prospekt mit Seitenangabe "S.<Nr>: <Text>"
- fondstyp_quelle: von welcher Hierarchie-Ebene die Information stammt

Suche primär auf SUBFONDS-Ebene (nicht Umbrella-Ebene).

=== AUFGABE 2 — Segmentierung pro Anteilsklasse ===

Für jede der nachfolgend aufgeführten ISINs/Anteilsklassen bestimme unabhängig \
die Investoren-Segmentierung.

SUCHSTRATEGIE (pro ISIN):
1. Suche den ISIN-Code wörtlich im Prospekttext (z.B. "LU0876692624")
2. Falls kein Treffer: Suche den Anteilsklassennamen in Tabellen oder Übersichten
3. Wenn weder ISIN noch Klassenname präzise gefunden: \
   setze info_quelle="nicht gefunden" und mindestanlage leer

SEGMENTIERUNGS-KATEGORIEN:
- retail: Privatanleger, öffentlicher Vertrieb, Minimum < 100'000 CHF/EUR
- institutional: Nur institutionelle/professionelle Anleger, Min. ≥ 500'000 CHF/EUR
- qualified: Qualifizierte Anleger (KAG Art.10/FIDLEG), Min. 100'000–499'999 CHF/EUR
- mixed: Keine klare Einschränkung, mehrere Anlegertypen adressiert
- unklar: Aus Prospektauszug nicht eindeutig bestimmbar

BEKANNTE ISINs IN DIESEM FONDS:
{isin_list}

=== AUSGABE (NUR JSON, kein weiterer Text) ===
{{
  "fondstyp":       "exakt ein Wert aus FONDSTYP-Liste",
  "fondstyp_roh":   "S.<Nr>: exakte Formulierung aus dem Prospekt",
  "fondstyp_quelle":"Subfonds|Umbrella|nicht gefunden",
  "anteilsklassen": [
    {{
      "isin":                 "ISIN oder leer",
      "anteilsklasse_name":   "Klassenname aus dem Prospekt",
      "info_quelle":          "ISIN-spezifisch|Anteilsklasse|Subfonds|Umbrella|nicht gefunden",
      "anlegertyp":           "exakt ein Wert aus ANLEGERTYP-Liste (leer wenn nicht gefunden)",
      "anlegertyp_roh":       "S.<Nr>: exakter Wortlaut",
      "kundentyp":            "exakt ein Wert aus KUNDENTYP-Liste (leer wenn nicht gefunden)",
      "kundentyp_roh":        "S.<Nr>: exakter Wortlaut",
      "segmentierung":        "retail|institutional|qualified|mixed|unklar",
      "begruendung":          "max. 200 Zeichen",
      "mindestanlage":        "Betrag + Währung (leer wenn nicht gefunden)",
      "mindestanlage_roh":    "S.<Nr>: exakter Wortlaut",
      "mindestanlage_quelle": "ISIN-spezifisch|Anteilsklasse|Subfonds|Umbrella|nicht gefunden"
    }}
  ]
}}\
"""


def _log(msg: str):
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


def step1_trim():
    """Kürzt das PDF per LLM und speichert .trimmed.txt."""
    trimmed_path = PDF_PATH.with_suffix(".trimmed.txt")
    if trimmed_path.exists():
        _log(f"Lösche alte trimmed.txt ({trimmed_path.stat().st_size:,} Bytes) …")
        trimmed_path.unlink()

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nicht in .env gefunden.")

    size_mb = PDF_PATH.stat().st_size / 1_048_576
    _log(f"Schritt 1: PDF trimmen — {PDF_PATH.name} ({size_mb:.1f} MB)")

    _log("  Tabellen extrahieren (alle Seiten) …")
    tables = pdf_analyzer.extract_tables_from_pdf(str(PDF_PATH), max_pages=None)
    pdf_analyzer.save_tables_json(str(PDF_PATH), tables)
    _log(f"  {len(tables)} Tabelle(n) gefunden und gespeichert.")

    _log("  Volltext extrahieren (alle Seiten, kein Limit) …")
    full_text = pdf_analyzer.extract_text_from_pdf(str(PDF_PATH), max_pages=None) or ""
    if not full_text:
        raise RuntimeError("Kein Text aus PDF extrahierbar.")
    _log(f"  {len(full_text):,} Zeichen extrahiert.")

    _CHUNK_MAX = 150_000   # ~37.5K Tokens — unter 50K/min-Rate-Limit
    _PAUSE_SEC = 62
    chunks = []
    start = 0
    while start < len(full_text):
        end = start + _CHUNK_MAX
        if end >= len(full_text):
            chunks.append(full_text[start:])
            break
        split_pos = full_text.rfind("\n\n", start, end)
        if split_pos <= start:
            split_pos = end
        else:
            split_pos += 2
        chunks.append(full_text[start:split_pos])
        start = split_pos

    _log(f"  Text in {len(chunks)} Chunk(s) aufgeteilt (je <= {_CHUNK_MAX:,} Zeichen)")

    client = anthropic.Anthropic(api_key=api_key)
    trimmed_parts = []
    for i, chunk in enumerate(chunks):
        _log(f"  Chunk {i + 1}/{len(chunks)}: {len(chunk):,} Zeichen -> LLM …")
        t_chunk_start = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=[{"type": "text", "text": TRIM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": chunk,
                 "cache_control": {"type": "ephemeral"}},
            ]}],
        )
        part = "".join(b.text for b in response.content if hasattr(b, "text"))
        _log(f"    -> {len(part):,} Zeichen")
        trimmed_parts.append(part)
        if i < len(chunks) - 1:
            wait = max(0.0, _PAUSE_SEC - (time.time() - t_chunk_start))
            if wait > 0:
                _log(f"  Rate-Limit-Pause {wait:.0f}s …")
                time.sleep(wait)

    trimmed = "\n\n".join(trimmed_parts)
    reduction = 100 - int(len(trimmed) / max(len(full_text), 1) * 100)
    _log(f"  Fertig: {len(full_text):,} -> {len(trimmed):,} Zeichen (-{reduction}%)")

    trimmed_path.write_text(trimmed, encoding="utf-8")
    _log(f"  Gespeichert: {trimmed_path.name}")


def _build_prompt():
    """Füllt ANALYSIS_PROMPT mit den Typologielisten."""
    fondstyp_liste   = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
    anlegertyp_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
    kundentyp_liste  = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
    return (
        ANALYSIS_PROMPT
        .replace("{fondstyp_liste}",   fondstyp_liste   or "  (keine Werte definiert)")
        .replace("{anlegertyp_liste}",  anlegertyp_liste or "  (keine Werte definiert)")
        .replace("{kundentyp_liste}",  kundentyp_liste  or "  (keine Werte definiert)")
    )


def step2_analyse():
    """Holt ISINs aus DB, gruppiert sie und startet LLM-Analyse."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nicht in .env gefunden.")

    results_store.init_db()
    all_rows = results_store.get_all_results()

    # Nur ISINs für dieses PDF, nach subfonds_id gruppieren
    groups: dict[str, list[dict]] = {}
    for row in all_rows:
        pfad = row.get("prospekt_pfad") or ""
        if "Goldman_Sachs_Emerging_Markets" not in pfad:
            continue
        key = row.get("subfonds_id") or f"__single_{row['isin']}"
        groups.setdefault(key, []).append(row)

    _log(f"\nSchritt 2: Analyse — {len(groups)} Gruppe(n), gesamt {sum(len(v) for v in groups.values())} ISINs")
    for k, v in groups.items():
        sf_name = (v[0].get("subfonds_name") or v[0].get("fondsname") or k)[:50]
        _log(f"  Gruppe '{sf_name}' — {len(v)} ISINs")

    prompt = _build_prompt()
    event_queue: queue.Queue = queue.Queue()

    worker = LLMAnalysisWorker(
        groups=groups,
        prompt_template=prompt,
        model="claude-sonnet-4-6",
        api_key=api_key,
        event_queue=event_queue,
        delay=0.5,
        workers=1,
        provider="anthropic",
    )
    worker.start()

    while True:
        try:
            evt: AnalysisEvent = event_queue.get(timeout=300)
            prefix = {
                "log":      "  LOG",
                "progress": "  OK ",
                "error":    "  ERR",
                "done":     "DONE",
            }.get(evt.type, "    ")
            _log(f"{prefix}: {evt.message}")
            if evt.type == "done":
                break
        except queue.Empty:
            _log("  … noch kein Event (warte weiter)")

    worker.join(timeout=10)
    _log("Analyse abgeschlossen.")


if __name__ == "__main__":
    _log("=== Goldman Sachs Emerging Markets — Trim + Analyse ===")
    step1_trim()
    step2_analyse()
    _log("=== Fertig ===")
