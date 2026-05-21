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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import anthropic

import pdf_analyzer
import results_store
from pdf_analyzer import extract_relevant_text
from utils import logger


_RATE_LIMIT_WAIT_SEC  = 2 * 3600   # 2 Stunden warten bei Rate Limit
_RATE_LIMIT_MAX_RETRIES = 6
_MAX_PDF_SIZE_MB = 30              # PDFs über diesem Limit werden übersprungen


class _RateLimitRetry(Exception):
    """Internes Signal: Rate Limit — Retry nach Wartezeit."""


def _format_tables_for_prompt(tables: list[dict]) -> str:
    """Formatiert extrahierte PDF-Tabellen als lesbaren Kontext für den LLM-Prompt."""
    lines = ["### ANTEILSKLASSEN-TABELLEN (strukturiert aus PDF):"]
    for t in tables:
        lines.append(f"\n--- Seite {t['page']} ---")
        lines.append(" | ".join(t.get("headers", [])))
        lines.append("-" * 40)
        for row in t.get("rows", []):
            lines.append(" | ".join(row))
    return "\n".join(lines)


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
        workers: int = 2,
        provider: str = "anthropic",
    ):
        super().__init__(daemon=True)
        self._groups = groups
        self._prompt_template = prompt_template
        self._model = model
        self._api_key = api_key
        self._provider = provider.lower()
        self._queue = event_queue
        self._delay = delay
        self._workers = max(1, workers)
        self._stop_flag = False
        self._done = 0
        self._failed = 0
        self._skipped = 0
        self._lock = threading.Lock()

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
        if self._provider == "gemini":
            return self._call_llm_gemini(pdf_text, prompt)
        if self._provider == "openrouter":
            return self._call_llm_openrouter(pdf_text, prompt)
        return self._call_llm_anthropic(pdf_text, prompt)

    def _call_llm_anthropic(self, pdf_text: str, prompt: str) -> dict | None:
        client = anthropic.Anthropic(api_key=self._api_key)
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=[{"type": "text", "text": prompt,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [
                    {"type": "text",
                     "text": f"### PROSPEKT-AUSZUG:\n\n{pdf_text[:80_000]}",
                     "cache_control": {"type": "ephemeral"}},
                ]}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Ungültiger API-Key — bitte im Admin konfigurieren.")
        except anthropic.RateLimitError:
            raise _RateLimitRetry()
        except anthropic.BadRequestError as exc:
            raise RuntimeError(f"Eingabe zu lang für Modell-Kontext — Prospekt kürzen: {exc}")
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"API-Fehler {exc.status_code}: {exc.message}")

        if response.stop_reason == "max_tokens":
            logger.warning("LLM-Ausgabe bei max_tokens abgeschnitten — JSON möglicherweise unvollständig.")

        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        return self._parse_response(raw)

    def _call_llm_gemini(self, pdf_text: str, prompt: str) -> dict | None:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise RuntimeError("google-genai nicht installiert. Bitte: pip install google-genai")

        client = genai.Client(api_key=self._api_key)
        try:
            response = client.models.generate_content(
                model=self._model,
                contents=f"### PROSPEKT-AUSZUG:\n\n{pdf_text[:80_000]}",
                config=types.GenerateContentConfig(
                    system_instruction=prompt,
                    max_output_tokens=8192,
                ),
            )
        except Exception as exc:
            msg = str(exc)
            if "UNAUTHENTICATED" in msg or "API_KEY_INVALID" in msg or "401" in msg:
                raise RuntimeError("Ungültiger Gemini API-Key — bitte im Admin konfigurieren.")
            if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower() or "429" in msg:
                raise _RateLimitRetry()
            raise RuntimeError(f"Gemini API-Fehler: {exc}")

        raw = response.text if hasattr(response, "text") else ""
        return self._parse_response(raw)

    def _call_llm_openrouter(self, pdf_text: str, prompt: str) -> dict | None:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai nicht installiert. Bitte: pip install openai")

        client = OpenAI(api_key=self._api_key, base_url="https://openrouter.ai/api/v1")
        try:
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=8192,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user",   "content": f"### PROSPEKT-AUSZUG:\n\n{pdf_text[:80_000]}"},
                ],
            )
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "invalid_api_key" in msg.lower() or "authentication" in msg.lower():
                raise RuntimeError("Ungültiger OpenRouter API-Key — bitte im Admin konfigurieren.")
            if "429" in msg or "rate_limit" in msg.lower() or "quota" in msg.lower():
                raise _RateLimitRetry()
            raise RuntimeError(f"OpenRouter API-Fehler: {exc}")

        raw = (response.choices[0].message.content or "") if response.choices else ""
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

        json_str = text[start:end]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        try:
            from json_repair import repair_json
            repaired = repair_json(json_str, return_objects=True)
            if isinstance(repaired, dict):
                logger.warning("JSON-Antwort wurde automatisch repariert (unescapte Anführungszeichen o.ä.).")
                return repaired
        except Exception:
            pass

        logger.error(f"JSON-Parsing fehlgeschlagen (auch nach Reparatur)\nAntwort: {text[:300]}")
        return None

    def _match_and_save(self, parsed: dict, group_rows: list[dict], model: str):
        """Matched LLM-Antwort auf ISINs und speichert Ergebnisse in DB."""
        fondstyp     = parsed.get("fondstyp",     "") or ""
        fondstyp_roh = parsed.get("fondstyp_roh", "") or ""
        klassen      = parsed.get("anteilsklassen", []) or []

        # Subfonds-Level Fallbacks für Anleger-/Kundentyp (Rückwärtskompatibilität
        # mit älteren Prompt-Versionen die noch Top-Level-Felder liefern)
        anleger_fallback     = parsed.get("anlegertyp",     "") or ""
        kunden_fallback      = parsed.get("kundentyp",      "") or ""
        anleger_roh_fallback = parsed.get("anlegertyp_roh", "") or ""
        kunden_roh_fallback  = parsed.get("kundentyp_roh",  "") or ""

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

        fondstyp_quelle = parsed.get("fondstyp_quelle", "") or ""

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
            begruendung       = (klassen_entry.get("begruendung")          or "") if klassen_entry else ""
            mindestanlage     = (klassen_entry.get("mindestanlage")        or "") if klassen_entry else ""
            mindestanlage_roh = (klassen_entry.get("mindestanlage_roh")    or "") if klassen_entry else ""
            info_quelle       = (klassen_entry.get("info_quelle")          or "") if klassen_entry else ""
            mindestanlage_q   = (klassen_entry.get("mindestanlage_quelle") or "") if klassen_entry else ""

            # Anleger-/Kundentyp: klassenspezifisch bevorzugen, Fallback auf Subfonds-Level
            anleger     = (klassen_entry.get("anlegertyp")     or "") if klassen_entry else ""
            anleger_roh = (klassen_entry.get("anlegertyp_roh") or "") if klassen_entry else ""
            kunden      = (klassen_entry.get("kundentyp")      or "") if klassen_entry else ""
            kunden_roh  = (klassen_entry.get("kundentyp_roh")  or "") if klassen_entry else ""
            if not anleger:
                anleger     = anleger_fallback
                anleger_roh = f"[Quelle: Subfonds] {anleger_roh_fallback}" if anleger_roh_fallback else ""
            if not kunden:
                kunden     = kunden_fallback
                kunden_roh = f"[Quelle: Subfonds] {kunden_roh_fallback}" if kunden_roh_fallback else ""

            # Mindestanlage leeren wenn Quelle übergeordnet
            if mindestanlage_q in ("Subfonds", "Umbrella", "nicht gefunden"):
                mindestanlage = ""

            # Quell-Präfix in _roh-Felder einbauen für Nachvollziehbarkeit
            if mindestanlage_q:
                mindestanlage_roh = (
                    f"[Quelle: {mindestanlage_q}] {mindestanlage_roh}"
                    if mindestanlage_roh else f"[Quelle: {mindestanlage_q}]"
                )
            if info_quelle and info_quelle != "ISIN-spezifisch":
                begruendung = f"[Quelle: {info_quelle}] {begruendung}"
            if fondstyp_quelle:
                ft_roh = f"[Quelle: {fondstyp_quelle}] {fondstyp_roh}" if fondstyp_roh else f"[Quelle: {fondstyp_quelle}]"
            else:
                ft_roh = fondstyp_roh

            results_store.update_llm_analysis(
                isin=isin,
                fondstyp=fondstyp,
                anlegertyp=anleger,
                kundentyp=kunden,
                llm_segmentierung=seg,
                llm_segmentierung_begruendung=begruendung[:400],
                fondstyp_roh=ft_roh[:200],
                anlegertyp_roh=anleger_roh[:200],
                kundentyp_roh=kunden_roh[:200],
                mindestanlage=mindestanlage[:100],
                mindestanlage_roh=mindestanlage_roh[:200],
                modell=model,
            )

    def _wait_interruptible(self, seconds: int, ref_isin: str, total: int) -> bool:
        """Schläft in 60s-Schritten; gibt False zurück wenn stop_flag gesetzt.
        Alle 10 Minuten erscheint ein Countdown-Log-Eintrag."""
        waited = 0
        while waited < seconds:
            if self._stop_flag:
                self._emit("log", isin=ref_isin,
                           message="Warten abgebrochen.", total=total)
                return False
            time.sleep(60)
            waited += 60
            remaining = (seconds - waited) // 60
            if remaining > 0 and waited % 600 == 0:
                self._emit("log", isin=ref_isin,
                           message=f"⏳ Noch {remaining} min bis Retry …", total=total)
        return True

    def _process_one(self, group_key: str, group_rows: list[dict], total: int):
        """Verarbeitet eine Subfonds-Gruppe — läuft in einem Thread-Pool-Worker."""
        if self._stop_flag:
            return

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
            with self._lock:
                self._skipped += 1
            self._emit("log", isin=ref_isin,
                       message=f"Kein PDF vorhanden — übersprungen ({ref_name})",
                       total=total)
            return

        size_mb = Path(pdf_path).stat().st_size / 1_048_576
        if size_mb > _MAX_PDF_SIZE_MB:
            with self._lock:
                self._skipped += 1
            self._emit("log", isin=ref_isin,
                       message=f"PDF zu groß ({size_mb:.1f} MB > {_MAX_PDF_SIZE_MB} MB) — übersprungen ({ref_name})",
                       total=total)
            return

        # Große PDFs ohne .trimmed.txt ablehnen — Analyse wäre unvollständig
        trimmed_path = Path(pdf_path).with_suffix(".trimmed.txt")
        if not trimmed_path.exists():
            pages = pdf_analyzer.get_pdf_metadata(pdf_path).get("pages", 0)
            if pages > pdf_analyzer._MAX_PAGES:
                with self._lock:
                    self._skipped += 1
                self._emit("log", isin=ref_isin,
                           message=(
                               f"PDF hat {pages} Seiten — bitte zuerst PDF-Kürzer verwenden, "
                               f"dann erneut analysieren ({ref_name})"
                           ),
                           total=total)
                return

        self._emit("log", isin=ref_isin,
                   message=f"Analysiere: {ref_name} ({len(group_rows)} ISINs) …",
                   total=total)

        # Text extrahieren
        try:
            pdf_text = extract_relevant_text(pdf_path) or ""
            if not pdf_text:
                raise ValueError("Kein Text extrahierbar")
            tables = pdf_analyzer.load_tables_json(pdf_path)
            if tables:
                pdf_text = _format_tables_for_prompt(tables) + "\n\n" + pdf_text
        except Exception as exc:
            with self._lock:
                self._failed += 1
            self._emit("error", isin=ref_isin,
                       message=f"PDF-Fehler: {exc}", total=total)
            return

        # LLM aufrufen — mit automatischem Retry bei Rate Limit
        parsed = None
        for attempt in range(1, _RATE_LIMIT_MAX_RETRIES + 1):
            try:
                parsed = self._call_llm(pdf_text, group_rows)
                break
            except _RateLimitRetry:
                if attempt == _RATE_LIMIT_MAX_RETRIES:
                    with self._lock:
                        self._failed += 1
                    self._emit("error", isin=ref_isin,
                               message=f"Rate Limit nach {attempt} Versuchen — Gruppe übersprungen",
                               total=total)
                    return
                h = _RATE_LIMIT_WAIT_SEC // 3600
                self._emit("log", isin=ref_isin,
                           message=(f"⏳ Rate Limit — warte {h}h "
                                    f"(Versuch {attempt}/{_RATE_LIMIT_MAX_RETRIES}) …"),
                           total=total)
                if not self._wait_interruptible(_RATE_LIMIT_WAIT_SEC, ref_isin, total):
                    return
                self._emit("log", isin=ref_isin,
                           message=f"🔄 Retry Versuch {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES} …",
                           total=total)
            except Exception as exc:
                with self._lock:
                    self._failed += 1
                self._emit("error", isin=ref_isin,
                           message=f"LLM-Fehler: {exc}", total=total)
                return

        if not parsed:
            with self._lock:
                self._failed += 1
            self._emit("error", isin=ref_isin,
                       message="LLM-Antwort konnte nicht geparst werden", total=total)
            return

        # Ergebnisse in DB schreiben
        try:
            self._match_and_save(parsed, group_rows, self._model)
        except Exception as exc:
            with self._lock:
                self._failed += 1
            self._emit("error", isin=ref_isin,
                       message=f"DB-Fehler: {exc}", total=total)
            return

        with self._lock:
            self._done += 1
        seg_summary = parsed.get("fondstyp", "?")
        self._emit("progress", isin=ref_isin,
                   message=f"{ref_name} → {seg_summary} | {len(group_rows)} ISINs gesetzt",
                   total=total)

    def run(self):
        try:
            total = len(self._groups)
            self._emit("log",
                       message=f"Starte LLM-Analyse: {total} Gruppe(n), {self._workers} Worker …",
                       total=total)

            with ThreadPoolExecutor(max_workers=self._workers) as executor:
                futures = {
                    executor.submit(self._process_one, key, rows, total): key
                    for key, rows in self._groups.items()
                }
                for future in as_completed(futures):
                    if self._stop_flag:
                        for f in futures:
                            f.cancel()
                    try:
                        future.result()
                    except Exception as exc:
                        self._emit("error", message=f"Unerwarteter Fehler: {exc}")

            stopped = self._stop_flag
            self._emit("done", message=(
                ("Abgebrochen. " if stopped else "Fertig. ") +
                f"Analysiert: {self._done}, "
                f"Übersprungen: {self._skipped}, Fehler: {self._failed}"
            ))
        except Exception as exc:
            self._emit("error", message=f"Worker-Fehler: {exc}")
            self._emit("done", message=f"Abgebrochen durch Fehler: {exc}")
