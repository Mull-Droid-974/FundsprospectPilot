"""
LLM-Analyse-Worker — analysiert Fondsprospekte per LLM.

Pro Subfonds-Gruppe (1 PDF) ein LLM-Aufruf, der alle Anteilsklassen/ISINs
der Gruppe klassifiziert. Ergebnisse werden direkt in der DB gespeichert.
"""

import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import anthropic

import results_store
import typologie_store
from utils import logger


_RATE_LIMIT_WAIT_SEC  = 70          # 70s warten bei Rate Limit (Tokens/Minute-Fenster)
_RATE_LIMIT_MAX_RETRIES = 6
_STOPPED = object()                 # Sentinel: Analyse wurde vom Nutzer abgebrochen


class _RateLimitRetry(Exception):
    """Internes Signal: Rate Limit — Retry nach Wartezeit."""


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


# ── Wiederverwendbare Helfer (Synchron- und Batch-Pfad teilen sich diese) ──────

def build_wert_maps() -> dict:
    """Pro Feld eine Map {kleingeschriebener Wert/Synonym → kanonischer Wert}
    für die Validierung der LLM-Ausgabe gegen die erlaubte Taxonomie."""
    maps: dict[str, dict[str, str]] = {}
    try:
        for feld in ("fondstyp", "anlegertyp", "kundentyp",
                     "dienstleistung", "vertriebskanal"):
            m: dict[str, str] = {}
            for entry in typologie_store.get_werte(feld):
                canon = (entry.get("wert") or "").strip()
                if not canon:
                    continue
                m[canon.lower()] = canon
                for syn in (entry.get("synonyme") or "").split(","):
                    syn = syn.strip().lower()
                    if syn:
                        m[syn] = canon
            maps[feld] = m
    except Exception as e:
        logger.warning(f"Taxonomie-Maps konnten nicht geladen werden: {e}")
    return maps


def normalize_wert(wert_maps: dict, feld: str, raw: str) -> str:
    """Mappt einen LLM-Wert auf den kanonischen Taxonomie-Wert.
    Leer wenn kein Treffer (fail-closed); unverändert wenn keine Map vorhanden (fail-open)."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = wert_maps.get(feld)
    if not m:
        return s
    hit = m.get(s.lower())
    if hit:
        return hit
    sl = s.lower()
    for key, canon in m.items():
        if key and (key in sl or sl in key):
            return canon
    logger.warning(f"[{feld}] LLM-Wert '{s}' nicht in Taxonomie — verworfen.")
    return ""


def parse_llm_json(text: str) -> dict | None:
    """Extrahiert das JSON-Objekt aus einer LLM-Antwort (mit json_repair-Fallback)."""
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


def build_isin_list(group_rows: list[dict]) -> str:
    lines = []
    for r in group_rows:
        isin = r.get("isin", "")
        klasse = r.get("anteilsklasse", "") or r.get("subfonds_name", "") or ""
        aussch = r.get("ausschuettungsart", "")
        waehr = r.get("fondswaehrung", "")
        detail = " | ".join(x for x in [klasse, aussch, waehr] if x)
        lines.append(f"  - {isin}: {detail}" if detail else f"  - {isin}")
    return "\n".join(lines)


def build_analysis_messages(prompt_template: str, pdf_document: dict,
                            group_rows: list[dict]) -> tuple[str, list]:
    """Baut (system_prompt, user_content) für einen Analyse-Call mit NATIVEM PDF.

    System-Prompt statisch (cachebar). user_content = [document-Block, Text mit
    ISIN-Liste] — das PDF steht VOR dem Text (Anthropic-Empfehlung)."""
    isin_list = build_isin_list(group_rows)
    system_prompt = prompt_template.replace(
        "{isin_list}",
        "(Die konkret zu analysierenden ISINs/Anteilsklassen stehen in der Nutzer-Nachricht unten.)",
    )
    user_content = [
        pdf_document,
        {"type": "text", "text": (
            f"### ZU ANALYSIERENDE ISINs / ANTEILSKLASSEN:\n{isin_list}\n\n"
            f"Klassifiziere die oben genannten ISINs/Anteilsklassen anhand des "
            f"beigefügten Prospekt-PDFs.")},
    ]
    return system_prompt, user_content


def merge_parsed_parts(parts: list[dict]) -> dict:
    """Führt geparste Ergebnisse mehrerer PDF-Teile EINER Gruppe zusammen (bei Split).

    Top-Level-Felder (fondstyp etc.): erster nicht-leerer Wert gewinnt.
    anteilsklassen: nach ISIN bzw. Name zusammenführen, nicht-leere Felder ergänzen;
    Reihenfolge bleibt erhalten."""
    if len(parts) == 1:
        return parts[0]

    merged: dict = {}
    for part in parts:
        for k, v in part.items():
            if k == "anteilsklassen":
                continue
            if v and not merged.get(k):
                merged[k] = v

    by_key: dict = {}
    order: list = []
    for part in parts:
        for kl in part.get("anteilsklassen", []) or []:
            key = ((kl.get("isin") or "").strip().upper()
                   or (kl.get("anteilsklasse_name") or "").strip().lower())
            if not key:
                order.append(("__anon%d__" % len(order), kl))
                continue
            if key not in by_key:
                by_key[key] = dict(kl)
                order.append((key, None))
            else:
                for kk, vv in kl.items():
                    if vv and not by_key[key].get(kk):
                        by_key[key][kk] = vv

    merged["anteilsklassen"] = [anon if anon is not None else by_key[key]
                                for key, anon in order]
    return merged


def match_and_save(parsed: dict, group_rows: list[dict], model: str, wert_maps: dict):
    """Matched LLM-Antwort auf ISINs und speichert Ergebnisse in DB (taxonomie-validiert)."""
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

        dienstleistung     = (klassen_entry.get("dienstleistung")     or "") if klassen_entry else ""
        dienstleistung_roh = (klassen_entry.get("dienstleistung_roh") or "") if klassen_entry else ""
        vertriebskanal     = (klassen_entry.get("vertriebskanal")     or "") if klassen_entry else ""
        vertriebskanal_roh = (klassen_entry.get("vertriebskanal_roh") or "") if klassen_entry else ""

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
            fondstyp=normalize_wert(wert_maps, "fondstyp", fondstyp),
            anlegertyp=normalize_wert(wert_maps, "anlegertyp", anleger),
            kundentyp=normalize_wert(wert_maps, "kundentyp", kunden),
            llm_segmentierung=seg,
            llm_segmentierung_begruendung=begruendung[:400],
            fondstyp_roh=ft_roh[:200],
            anlegertyp_roh=anleger_roh[:200],
            kundentyp_roh=kunden_roh[:200],
            mindestanlage=mindestanlage[:100],
            mindestanlage_roh=mindestanlage_roh[:200],
            dienstleistung=normalize_wert(wert_maps, "dienstleistung", dienstleistung)[:100],
            dienstleistung_roh=dienstleistung_roh[:200],
            vertriebskanal=normalize_wert(wert_maps, "vertriebskanal", vertriebskanal)[:100],
            vertriebskanal_roh=vertriebskanal_roh[:200],
            modell=model,
        )


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
        self._wert_maps = build_wert_maps()

    def _normalize_wert(self, feld: str, raw: str) -> str:
        return normalize_wert(self._wert_maps, feld, raw)

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
        return build_isin_list(group_rows)

    def _call_llm(self, pdf_document: dict, group_rows: list[dict]) -> dict | None:
        system_prompt, user_content = build_analysis_messages(
            self._prompt_template, pdf_document, group_rows
        )
        if self._provider != "anthropic":
            raise RuntimeError(
                "Native-PDF-Analyse wird nur für Anbieter 'anthropic' unterstützt."
            )
        return self._call_llm_anthropic(user_content, system_prompt)

    def _call_llm_anthropic(self, user_content: list, system_prompt: str) -> dict | None:
        client = anthropic.Anthropic(api_key=self._api_key)
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=16384,
                # cache_control nur auf dem statischen System-Prompt → Cache-Treffer ab Call 2.
                system=[{"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}}],
                # User-Nachricht (PDF + ISINs) ist pro Gruppe unique → kein cache_control.
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Ungültiger API-Key — bitte im Admin konfigurieren.")
        except anthropic.RateLimitError:
            raise _RateLimitRetry()
        except anthropic.BadRequestError as exc:
            # BadRequestError kann verschiedene Ursachen haben — zeige den echten Fehler
            exc_str = str(exc).lower()
            if "usage" in exc_str or "limit" in exc_str or "regain access" in exc_str:
                raise RuntimeError(f"🚫 API-Limit erreicht. Zugriff wiederhergestellt am: 2026-07-01")
            else:
                raise RuntimeError(f"Eingabe zu lang für Modell-Kontext — Prospekt kürzen: {exc}")
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"API-Fehler {exc.status_code}: {exc.message}")

        if response.stop_reason == "max_tokens":
            logger.warning(
                "LLM-Ausgabe bei max_tokens (16384) abgeschnitten — JSON evtl. unvollständig. "
                "Subfonds mit sehr vielen Anteilsklassen ggf. aufteilen."
            )

        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        return self._parse_response(raw)

    def _parse_response(self, text: str) -> dict | None:
        return parse_llm_json(text)

    def _match_and_save(self, parsed: dict, group_rows: list[dict], model: str):
        match_and_save(parsed, group_rows, model, self._wert_maps)

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

        self._emit("log", isin=ref_isin,
                   message=f"Analysiere: {ref_name} ({len(group_rows)} ISINs) …",
                   total=total)

        # Natives PDF aufbereiten (ganzes PDF, reduziert, oder in Teile gesplittet)
        try:
            from pdf_native import prepare_pdf_documents
            parts = prepare_pdf_documents(pdf_path)
            if not parts:
                raise ValueError("PDF nicht aufbereitbar")
        except Exception as exc:
            with self._lock:
                self._failed += 1
            self._emit("error", isin=ref_isin,
                       message=f"PDF-Fehler: {exc}", total=total)
            return

        n_parts = parts[0]["n_parts"]
        if n_parts > 1:
            self._emit("log", isin=ref_isin,
                       message=f"{ref_name}: {n_parts} native Teile → einzeln analysieren + mergen",
                       total=total)

        # Jeden Teil mit Rate-Limit-Retry analysieren, dann mergen
        parsed_parts = []
        for part in parts:
            res = self._call_with_retry(part["block"], group_rows, ref_isin, total)
            if res is _STOPPED:
                return
            if res is None:
                if n_parts > 1:
                    self._emit("log", isin=ref_isin,
                               message=f"Teil {part['part']}/{n_parts} fehlgeschlagen — Rest wird verwendet",
                               total=total)
                    continue
                with self._lock:
                    self._failed += 1
                self._emit("error", isin=ref_isin,
                           message="LLM-Antwort konnte nicht abgerufen/geparst werden", total=total)
                return
            parsed_parts.append(res)

        if not parsed_parts:
            with self._lock:
                self._failed += 1
            self._emit("error", isin=ref_isin, message="Keine verwertbare Antwort", total=total)
            return

        merged = merge_parsed_parts(parsed_parts)

        # Ergebnisse in DB schreiben
        try:
            self._match_and_save(merged, group_rows, self._model)
        except Exception as exc:
            with self._lock:
                self._failed += 1
            self._emit("error", isin=ref_isin,
                       message=f"DB-Fehler: {exc}", total=total)
            return

        with self._lock:
            self._done += 1
        seg_summary = merged.get("fondstyp", "?")
        self._emit("progress", isin=ref_isin,
                   message=f"{ref_name} → {seg_summary} | {len(group_rows)} ISINs gesetzt",
                   total=total)

    def _call_with_retry(self, pdf_document, group_rows, ref_isin, total):
        """LLM-Aufruf für einen (Teil-)Block mit Rate-Limit-Retry.
        Rückgabe: geparstes dict | None (endgültig fehlgeschlagen) | _STOPPED (abgebrochen)."""
        for attempt in range(1, _RATE_LIMIT_MAX_RETRIES + 1):
            try:
                return self._call_llm(pdf_document, group_rows)
            except _RateLimitRetry:
                if attempt == _RATE_LIMIT_MAX_RETRIES:
                    self._emit("log", isin=ref_isin,
                               message=f"Rate Limit nach {attempt} Versuchen", total=total)
                    return None
                self._emit("log", isin=ref_isin,
                           message=f"⏳ Rate Limit — warte "
                                   f"(Versuch {attempt}/{_RATE_LIMIT_MAX_RETRIES}) …", total=total)
                if not self._wait_interruptible(_RATE_LIMIT_WAIT_SEC, ref_isin, total):
                    return _STOPPED
                self._emit("log", isin=ref_isin,
                           message=f"🔄 Retry Versuch {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES} …",
                           total=total)
            except Exception as exc:
                self._emit("error", isin=ref_isin, message=f"LLM-Fehler: {exc}", total=total)
                return None
        return None

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
