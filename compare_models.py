"""
A/B-Vergleich: Sonnet vs. Haiku für die Prospekt-Analyse.

Nimmt N Subfonds-Gruppen mit vorhandenem PDF, analysiert jede mit beiden
Modellen und vergleicht die 5 Klassifizierungsfelder pro ISIN.

Ausgabe:
- data/output/model_compare.csv  (alle Werte nebeneinander)
- Übereinstimmungsquote pro Feld auf der Konsole

Aufruf:  python compare_models.py [ANZAHL_GRUPPEN]   (Default 15)

Nutzt KEINE Batch-API und schreibt NICHT in die Ergebnis-DB.
"""

import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import anthropic
from dotenv import load_dotenv

import results_store
import typologie_store
from llm_analysis_worker import (
    build_analysis_messages, parse_llm_json, prepare_pdf_text,
    build_wert_maps, normalize_wert, _normalize_seg,
)
from prospekt_analysis_window import DEFAULT_PROMPT

load_dotenv()

SONNET = "claude-sonnet-4-6"
HAIKU  = "claude-haiku-4-5-20251001"
FIELDS = ["fondstyp", "anlegertyp", "kundentyp", "dienstleistung", "vertriebskanal"]


def build_taxonomy_prompt() -> str:
    """DEFAULT_PROMPT mit den Taxonomie-Listen befüllt (ohne {isin_list})."""
    def liste(feld):
        return "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste(feld)) or "  (keine)"
    return (
        DEFAULT_PROMPT
        .replace("{fondstyp_liste}",       liste("fondstyp"))
        .replace("{anlegertyp_liste}",      liste("anlegertyp"))
        .replace("{kundentyp_liste}",       liste("kundentyp"))
        .replace("{dienstleistung_liste}",  liste("dienstleistung"))
        .replace("{vertriebskanal_liste}",  liste("vertriebskanal"))
    )


def call_model(model: str, system_prompt: str, user_text: str, api_key: str) -> dict | None:
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=16384,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return parse_llm_json(raw)


def fields_for_row(parsed: dict, row: dict, wert_maps: dict) -> dict:
    """Extrahiert die 5 (taxonomie-validierten) Felder für eine ISIN aus der LLM-Antwort."""
    klassen = (parsed or {}).get("anteilsklassen", []) or []
    by_isin, by_name = {}, {}
    for k in klassen:
        ki = (k.get("isin") or "").strip().upper()
        kn = (k.get("anteilsklasse_name") or "").strip().lower()
        if ki:
            by_isin[ki] = k
        if kn:
            by_name[kn] = k
    isin = row["isin"]
    entry = (by_isin.get(isin.upper())
             or by_name.get((row.get("anteilsklasse") or "").strip().lower())
             or (klassen[0] if len(klassen) == 1 else {})) or {}
    fondstyp = (parsed or {}).get("fondstyp", "") or ""
    return {
        "fondstyp":       normalize_wert(wert_maps, "fondstyp", fondstyp),
        "anlegertyp":     normalize_wert(wert_maps, "anlegertyp", entry.get("anlegertyp", "") or (parsed or {}).get("anlegertyp", "")),
        "kundentyp":      normalize_wert(wert_maps, "kundentyp", entry.get("kundentyp", "") or (parsed or {}).get("kundentyp", "")),
        "dienstleistung": normalize_wert(wert_maps, "dienstleistung", entry.get("dienstleistung", "")),
        "vertriebskanal": normalize_wert(wert_maps, "vertriebskanal", entry.get("vertriebskanal", "")),
    }


def main():
    n_groups = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("FEHLER: ANTHROPIC_API_KEY fehlt in .env")
        return

    trim_model = HAIKU
    wert_maps = build_wert_maps()
    prompt = build_taxonomy_prompt()

    # Gruppen bilden (subfonds_id), nur mit vorhandenem PDF
    raw = {}
    for row in results_store.get_all_results():
        key = row.get("subfonds_id") or f"__single_{row['isin']}"
        raw.setdefault(key, []).append(row)

    groups = [
        (k, rows) for k, rows in raw.items()
        if any(r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists() for r in rows)
    ][:n_groups]

    if not groups:
        print("Keine Gruppen mit vorhandenem PDF gefunden.")
        return

    print(f"Vergleiche {len(groups)} Gruppe(n) — Sonnet vs. Haiku …\n")

    out_path = Path("data/output/model_compare.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    agree = {f: 0 for f in FIELDS}
    total_rows = 0

    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        header = ["subfonds_id", "isin"]
        for f in FIELDS:
            header += [f"{f}_sonnet", f"{f}_haiku", f"{f}_gleich"]
        writer.writerow(header)

        for gi, (key, rows) in enumerate(groups, 1):
            pdf_path = next(r["prospekt_pfad"] for r in rows
                            if r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists())
            print(f"[{gi}/{len(groups)}] {Path(pdf_path).name} ({len(rows)} ISIN) …")

            pdf_text = prepare_pdf_text(pdf_path, trim_model=trim_model,
                                        api_key=api_key, provider="anthropic")
            if not pdf_text:
                print("    übersprungen (kein Text)")
                continue

            system_prompt, user_text = build_analysis_messages(prompt, pdf_text, rows)
            try:
                p_sonnet = call_model(SONNET, system_prompt, user_text, api_key)
                p_haiku  = call_model(HAIKU,  system_prompt, user_text, api_key)
            except Exception as e:
                print(f"    LLM-Fehler: {e}")
                continue

            for row in rows:
                fs = fields_for_row(p_sonnet, row, wert_maps)
                fh_ = fields_for_row(p_haiku, row, wert_maps)
                line = [key, row["isin"]]
                for f in FIELDS:
                    same = (fs[f] == fh_[f])
                    if same:
                        agree[f] += 1
                    line += [fs[f], fh_[f], "✓" if same else "✗"]
                writer.writerow(line)
                total_rows += 1

    print(f"\nGeschrieben: {out_path}  ({total_rows} ISIN-Zeilen)\n")
    print("Übereinstimmung Sonnet vs. Haiku pro Feld:")
    for f in FIELDS:
        pct = (agree[f] / total_rows * 100) if total_rows else 0
        print(f"  {f:16s}: {agree[f]:4d}/{total_rows}  ({pct:.0f}%)")


if __name__ == "__main__":
    main()
