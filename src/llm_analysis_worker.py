"""
LLM-Analyse-Worker — analysiert Fondsprospekte per LLM.

Pro Subfonds-Gruppe (1 PDF) ein LLM-Aufruf, der alle Anteilsklassen/ISINs
der Gruppe klassifiziert. Ergebnisse werden direkt in der DB gespeichert.
"""

import json
import queue
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import anthropic

import results_store
from pdf_analyzer import extract_relevant_text
from utils import logger


@dataclass
class AnalysisEvent:
    type: str        # "log" | "progress" | "error" | "done"
    isin: str = ""
    message: str = ""
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0


# Segmentierungs-Normalisierung
_SEG_MAP = {
    "retail":        "retail",
    "privat":        "retail",
    "institutional": "institutional",
    "institutionell":"institutional",
    "qualified":     "qualified",
    "qualifiziert":  "qualified",
    "qualified investor": "qualified",
    "mixed":         "mixed",
    "gemischt":      "mixed",
}


def _normalize_seg(raw: str) -> str:
    s = (raw or "").lower().strip()
    for k, v in _SEG_MAP.items():
        if k in s:
            return v
    return "unklar"


class LLMAnalysisWorker(threading.Thread):
    """
    Analysiert Subfonds-Gruppen per LLM.
    groups: {group_key: [row, ...]} — pro Gruppe ein LLM-Aufruf.
    """

    def __init__(
        self,
        groups: dict,
        prompt_template: str,
        model: str,
        api_key: str,
        event_queue: queue.Queue,
        delay: float = 0.5,
    ):
        super().__init__(daemon=True)
        self._groups = groups
        self._prompt_template = prompt_template
        self._model = model
        self._api_key = api_key
        self._queue = event_queue
        self._delay = delay
        self._stop_flag = False
        self._done = 0
        self._failed = 0
        self._skipped = 0

    def stop(self):
        self._stop_flag = True

    def _emit(self, type_: str, isin: str = "", message: str = "", total: int = 0):
        self._queue.put(AnalysisEvent(
            type=type_,
            isin=isin,
            message=message,
            total=total or len(self._groups),
            done=self._done,
            failed=self._failed,
            skipped=self._skipped,
        ))

    def _build_isin_list(self, group_rows: list[dict]) -> str:
        lines = []
        for r in group_rows:
            isin = r.get("isin", "")
            klasse = r.get("anteilsklasse", "") or r.get("subfonds_name", "") or ""
            aussch = r.get("ausschuettungsart", "")
            waehr = r.get("fondswaehrung", "")
            detail = " | ".join(x for x in [klasse, aussch, waehr] if x)
            lines.append(f"  - {isin}: {detail}" if detail else f"  - {isin}")
        return "\n".join(lines)

    def _call_llm(self, pdf_text: str, group_rows: list[dict]) -> dict | None:
        isin_list = self._build_isin_list(group_rows)
        prompt = self._prompt_template.replace("{isin_list}", isin_list)
        user_msg = f"{prompt}\n\n### PROSPEKT-AUSZUG:\n\n{pdf_text[:80_000]}"

        client = anthropic.Anthropic(api_key=self._api_key)
        response = client.messages.create(
            model=self._model,
            max_tokens=2048,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        return self._parse_response(raw)

    def _parse_response(self, text: str) -> dict | None:
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            return None

        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError as e:
            logger.error(f"JSON-Parsing fehlgeschlagen: {e}\nAntwort: {text[:300]}")
            return None

    def _match_and_save(self, parsed: dict, group_rows: list[dict], model: str):
        """Matched LLM-Antwort auf ISINs und speichert Ergebnisse in DB."""
        fondstyp      = parsed.get("fondstyp",      "") or ""
        anleger       = parsed.get("anlegertyp",    "") or ""
        kunden        = parsed.get("kundentyp",     "") or ""
        fondstyp_roh  = parsed.get("fondstyp_roh",  "") or ""
        anleger_roh   = parsed.get("anlegertyp_roh","") or ""
        kunden_roh    = parsed.get("kundentyp_roh", "") or ""
        klassen       = parsed.get("anteilsklassen", []) or []

        # Index: isin → klassen-entry, anteilsklasse_name → klassen-entry
        by_isin: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        for k in klassen:
            ki = (k.get("isin") or "").strip().upper()
            kn = (k.get("anteilsklasse_name") or "").strip().lower()
            if ki:
                by_isin[ki] = k
            if kn:
                by_name[kn] = k

        for row in group_rows:
            isin = row["isin"]
            db_klasse = (row.get("anteilsklasse") or "").strip().lower()

            klassen_entry = (
                by_isin.get(isin.upper())
                or by_name.get(db_klasse)
                or (klassen[0] if len(klassen) == 1 else None)
            )

            seg = _normalize_seg(
                klassen_entry.get("segmentierung", "") if klassen_entry else ""
            )
            begruendung = (
                (klassen_entry.get("begruendung") or "") if klassen_entry else ""
            )

            results_store.update_llm_analysis(
                isin=isin,
                fondstyp=fondstyp,
                anlegertyp=anleger,
                kundentyp=kunden,
                llm_segmentierung=seg,
                llm_segmentierung_begruendung=begruendung[:400],
                fondstyp_roh=fondstyp_roh[:200],
                anlegertyp_roh=anleger_roh[:200],
                kundentyp_roh=kunden_roh[:200],
                modell=model,
            )

    def run(self):
        try:
            total = len(self._groups)
            self._emit("log", message=f"Starte LLM-Analyse für {total} Subfonds-Gruppe(n) …",
                       total=total)

            for group_key, group_rows in self._groups.items():
                if self._stop_flag:
                    self._emit("log", message="Analyse abgebrochen.")
                    break

                ref_isin = group_rows[0]["isin"] if group_rows else ""
                ref_name = (
                    group_rows[0].get("subfonds_name") or
                    group_rows[0].get("fondsname") or
                    group_key
                )

                # PDF finden
                pdf_path = next(
                    (r["prospekt_pfad"] for r in group_rows
                     if r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists()),
                    None,
                )
                if not pdf_path:
                    self._skipped += 1
                    self._emit("log", isin=ref_isin,
                               message=f"Kein PDF vorhanden — übersprungen ({ref_name})",
                               total=total)
                    continue

                self._emit("log", isin=ref_isin,
                           message=f"Analysiere: {ref_name} ({len(group_rows)} ISINs) …",
                           total=total)

                # Text extrahieren
                try:
                    pdf_text = extract_relevant_text(pdf_path) or ""
                    if not pdf_text:
                        raise ValueError("Kein Text extrahierbar")
                except Exception as exc:
                    self._failed += 1
                    self._emit("error", isin=ref_isin,
                               message=f"PDF-Fehler: {exc}", total=total)
                    continue

                # LLM aufrufen
                try:
                    parsed = self._call_llm(pdf_text, group_rows)
                    if not parsed:
                        raise ValueError("LLM-Antwort konnte nicht geparst werden")
                except Exception as exc:
                    self._failed += 1
                    self._emit("error", isin=ref_isin,
                               message=f"LLM-Fehler: {exc}", total=total)
                    continue

                # Ergebnisse in DB schreiben
                try:
                    self._match_and_save(parsed, group_rows, self._model)
                except Exception as exc:
                    self._failed += 1
                    self._emit("error", isin=ref_isin,
                               message=f"DB-Fehler: {exc}", total=total)
                    continue

                self._done += 1
                seg_summary = parsed.get("fondstyp", "?")
                self._emit("progress", isin=ref_isin,
                           message=f"{ref_name} → {seg_summary} | {len(group_rows)} ISINs gesetzt",
                           total=total)

            self._emit("done", message=(
                f"Fertig. Analysiert: {self._done}, "
                f"Übersprungen: {self._skipped}, Fehler: {self._failed}"
            ))
        except Exception as exc:
            self._emit("error", message=f"Worker-Fehler: {exc}")
            self._emit("done", message=f"Abgebrochen durch Fehler: {exc}")
