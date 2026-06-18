"""
PDF-Trim-Maske — Intelligentes Kürzen / Extrahieren von Fondsprospekten per LLM.

Liest alle PDFs aus data/prospekte/ und ermöglicht:
- Strukturierte Tabellenextraktion  (→ .tables.json neben dem PDF)
- LLM-gestütztes Kürzen            (→ .trimmed.txt neben dem PDF)
- LLM-gestützte JSON-Extraktion    (→ .extracted.json neben dem PDF)
  Chunk-weise Extraktion ohne Informationsverlust, bevorzugt von der
  Analyse-Pipeline gegenüber .trimmed.txt.

Original-PDF bleibt immer unangetastet.
"""

import json
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
import logging
from dotenv import load_dotenv

import pdf_analyzer
import results_store

load_dotenv()

logger = logging.getLogger(__name__)

# ─── Farben ──────────────────────────────────────────────────────────────────
BG_MAIN         = "#1e1e2e"
BG_PANEL        = "#2a2a3e"
BG_INPUT        = "#313145"
FG_TEXT         = "#cdd6f4"
FG_MUTED        = "#7f849c"
ACCENT_BLUE     = "#89b4fa"
ACCENT_GREEN    = "#a6e3a1"
ACCENT_RED      = "#f38ba8"
ACCENT_YELLOW   = "#f9e2af"
ACCENT_LAVENDER = "#b4befe"
BTN_BG          = "#45475a"
BTN_ACTIVE      = "#585b70"

_PDF_FOLDER = Path(__file__).parent.parent / "data" / "prospekte"

_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]

_DEFAULT_PROMPT = """\
Kürze den folgenden Prospekt-Text auf die für die Anteilsklassen-Klassifizierung \
relevanten Abschnitte.

BEHALTEN (vollständig):
- Abschnitts-Überschriften und Fondsnamen (Umbrella-Fonds, Subfonds) als struktureller Kontext
- Anteilsklassen-Tabellen mit ISIN, Mindestanlage, Währung, TER
- Anleger-Beschränkungen und Zulassungskriterien
- Regulatorische Klassifizierungen (MiFID, KAG/FIDLEG, UCITS, AIFMD)
- Dienstleistungstyp: Delegation, Execution only, Beratung, Vermögensmanagement
- Vertriebskanal: direkt, Captive Channel, Finanzintermediäre, Vermittler
- Vertriebsbeschränkungen nach Land / Investorentyp
- Abschnitte zu "Qualified Investors", "Professional Investors", "Retail", "Accredited"

ENTFERNEN (weglassen):
- Allgemeine Risikobeschreibungen ohne Bezug zu Anlegertypen
- Verwaltungsdetails (Depotbank, Revisoren, Zeichnungsfristen ohne Mindestanlage)
- Historische Performance-Daten
- Allgemeine Steuerhinweise
- Detaillierte Vermögensallokation (nur Struktur-Übersicht behalten)

Gib NUR den gekürzten Text zurück — kein Kommentar, keine Erklärung, \
keine Markdown-Formatierung.\
"""


# ─── Chunking ────────────────────────────────────────────────────────────────

_CHUNK_MAX_CHARS = 150_000   # ~37.5K Tokens — unter dem 50K/min-Rate-Limit
_CHUNK_MIN_INTERVAL = 62     # Mindest-Abstand (s) zwischen Chunk-Starts (Rate-Limit)


def _chunk_text(text: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """Teilt Text in Chunks ≤ max_chars, bevorzugt Absatz-Grenzen (\n\n)."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:])
            break
        split_pos = text.rfind("\n\n", start, end)
        if split_pos <= start:
            split_pos = end
        else:
            split_pos += 2
        chunks.append(text[start:split_pos])
        start = split_pos
    return chunks


# ─── Extraktion: Prompt + Merge-Logik ────────────────────────────────────────

_EXTRACT_PROMPT = """\
Du bist ein Experte für Fondsprospekte (Schweizer KAG/FIDLEG, UCITS, AIFMD, MiFID II).

Analysiere den folgenden Prospekt-Abschnitt und extrahiere alle relevanten Informationen \
in der nachfolgenden JSON-Struktur. Extrahiere NUR was direkt im Text steht.

HIERARCHIE:
1. UMBRELLA: Dachfonds-Ebene (Name, Rechtsform, Domizil, Regulierung)
2. PORTFOLIO: Subfonds-Ebene (Anlageziel, Fondstyp, Anleger-Beschränkungen)
3. ANTEILSKLASSE: Klassenspezifische Details (ISIN, Mindestanlage, Anlegertyp)

REGELN:
- Felder die nicht im Text vorkommen: leer lassen ("" oder [])
- Anteilsklassen nur wenn ISIN oder Klassenname explizit im Text steht
- Prosa-Texte kurz zusammenfassen (max. 200 Zeichen)
- Irrelevantes ignorieren: Steuerhinweise, Performance, allgemeine Risikobeschreibungen \
  ohne Anlegerbezug, Verwaltungsdetails ohne Bezug zu Anlegertypen
- Gib NUR gültiges JSON zurück, keinen anderen Text
- Falls keine relevanten Informationen: {}

AUSGABE-SCHEMA:
{
  "umbrella": {
    "name": "",
    "rechtsform": "",
    "domizil": "",
    "regulierung": "",
    "verwaltungsgesellschaft": "",
    "allg_anleger_beschraenkung": ""
  },
  "portfolios": [
    {
      "name": "",
      "fondstyp": "",
      "anlageziel_kurz": "",
      "anleger_beschraenkung": "",
      "mifid_kategorie": "",
      "anteilsklassen": [
        {
          "isin": "",
          "klasse_name": "",
          "waehrung": "",
          "mindestanlage": "",
          "anlegertyp_beschraenkung": "",
          "vertrieb_laender": [],
          "ter": "",
          "ausschuettung": ""
        }
      ]
    }
  ]
}\
"""


def _merge_extracted(base: dict, new: dict) -> dict:
    """Führt zwei chunk-weise extrahierte JSON-Dicts zusammen (non-destructive merge)."""
    if not new:
        return base
    if not base:
        return dict(new)

    # Umbrella: leere Felder mit neuen Werten befüllen
    if new.get("umbrella"):
        if "umbrella" not in base:
            base["umbrella"] = {}
        for k, v in new["umbrella"].items():
            if v and not base["umbrella"].get(k):
                base["umbrella"][k] = v

    # Portfolios: nach Name zusammenführen
    for new_pf in (new.get("portfolios") or []):
        pf_name = (new_pf.get("name") or "").strip().lower()
        matched_pf = None
        for base_pf in (base.get("portfolios") or []):
            base_name = (base_pf.get("name") or "").strip().lower()
            if base_name and pf_name and (
                base_name == pf_name or
                base_name in pf_name or
                pf_name in base_name
            ):
                matched_pf = base_pf
                break

        if matched_pf is None:
            base.setdefault("portfolios", []).append(new_pf)
        else:
            # Portfolio-Felder ergänzen
            for k, v in new_pf.items():
                if k == "anteilsklassen":
                    continue
                if v and not matched_pf.get(k):
                    matched_pf[k] = v

            # Anteilsklassen zusammenführen
            for new_kl in (new_pf.get("anteilsklassen") or []):
                new_isin  = (new_kl.get("isin")        or "").strip().upper()
                new_kname = (new_kl.get("klasse_name") or "").strip().lower()
                matched_kl = None
                for base_kl in matched_pf.setdefault("anteilsklassen", []):
                    base_isin  = (base_kl.get("isin")        or "").strip().upper()
                    base_kname = (base_kl.get("klasse_name") or "").strip().lower()
                    if (new_isin and base_isin and new_isin == base_isin) or \
                       (new_kname and base_kname and new_kname == base_kname):
                        matched_kl = base_kl
                        break

                if matched_kl is None:
                    matched_pf["anteilsklassen"].append(new_kl)
                else:
                    for k, v in new_kl.items():
                        if k == "vertrieb_laender":
                            existing = matched_kl.get("vertrieb_laender") or []
                            merged   = list(dict.fromkeys(existing + (v or [])))
                            if merged:
                                matched_kl["vertrieb_laender"] = merged
                        elif v and not matched_kl.get(k):
                            matched_kl[k] = v
    return base


def _run_trim_headless(pdf_path: str, model: str, api_key: str, provider: str = "anthropic") -> bool:
    """
    Kürzt PDF zu .trimmed.txt ohne GUI/Queue-Overhead.
    Günstiger als Extraktion — nur relevante Abschnitte.
    Rückgabe: True wenn erfolgreich, False bei Fehler.
    """
    import time as _time

    try:
        pdf_path = Path(pdf_path)
        out = pdf_path.with_suffix(".trimmed.txt")
        if out.exists():
            return True

        full_text = pdf_analyzer.extract_text_from_pdf(str(pdf_path), max_pages=None) or ""
        if not full_text:
            logger.warning(f"[{pdf_path.name}] Kein Text extrahierbar")
            return False

        # Gratis-Vorfilter: nur klassifizierungsrelevante Abschnitte an die LLM schicken
        # (reduziert die Trim-Eingabe von ~400K auf ≤80K Zeichen). Tabellen sind separat
        # in .tables.json gesichert; bei zu wenig Treffern fällt der Filter auf den
        # Dokumentanfang zurück.
        from utils import extract_relevant_sections
        orig_len = len(full_text)
        full_text = extract_relevant_sections(full_text)
        logger.info(f"[{pdf_path.name}] Vorfilter: {orig_len:,} → {len(full_text):,} Zeichen")

        chunks = _chunk_text(full_text)
        logger.info(f"[{pdf_path.name}] Trimme {len(chunks)} Chunk(s)")

        trimmed_parts = []
        for i, chunk in enumerate(chunks):
            t_start = _time.time()
            try:
                if provider.lower() == "gemini":
                    try:
                        from google import genai
                        from google.genai import types
                    except ImportError:
                        logger.error("google-genai nicht installiert")
                        return False
                    client = genai.Client(api_key=api_key)
                    resp = client.models.generate_content(
                        model=model,
                        contents=chunk,
                        config=types.GenerateContentConfig(
                            system_instruction=_DEFAULT_PROMPT,
                            max_output_tokens=8192,
                        ),
                    )
                    trimmed = resp.text if hasattr(resp, "text") else ""
                elif provider.lower() == "openrouter":
                    try:
                        from openai import OpenAI
                    except ImportError:
                        logger.error("openai nicht installiert")
                        return False
                    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
                    resp = client.chat.completions.create(
                        model=model,
                        max_tokens=8192,
                        messages=[
                            {"role": "system", "content": _DEFAULT_PROMPT},
                            {"role": "user", "content": chunk},
                        ],
                    )
                    trimmed = (resp.choices[0].message.content or "") if resp.choices else ""
                else:  # anthropic
                    client = anthropic.Anthropic(api_key=api_key)
                    resp = client.messages.create(
                        model=model,
                        max_tokens=8192,
                        system=[{"type": "text", "text": _DEFAULT_PROMPT,
                                 "cache_control": {"type": "ephemeral"}}],
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": chunk[:80_000],
                             "cache_control": {"type": "ephemeral"}},
                        ]}],
                    )
                    trimmed = "".join(b.text for b in resp.content if hasattr(b, "text"))

                if trimmed:
                    trimmed_parts.append(trimmed)
            except Exception as exc:
                logger.error(f"[{pdf_path.name}] Chunk {i+1} Fehler: {exc}")
                return False

            if len(chunks) > 1 and i < len(chunks) - 1:
                wait = max(0.0, _CHUNK_MIN_INTERVAL - (_time.time() - t_start))
                if wait > 0:
                    _time.sleep(wait)

        # Speichern
        result_text = "\n\n".join(trimmed_parts)
        out.write_text(result_text, encoding="utf-8")
        logger.info(f"[{pdf_path.name}] Trimmen fertig → {out.name} ({len(result_text):,} Zeichen)")
        return True
    except Exception as exc:
        logger.error(f"Auto-Trimmen Error: {exc}")
        return False


def _run_extraction_headless(pdf_path: str, model: str, api_key: str, provider: str = "anthropic") -> bool:
    """
    Extrahiert PDF zu .extracted.json ohne GUI/Queue-Overhead.
    Verwendet für Auto-Extraktion in der Batch-Analyse.
    Rückgabe: True wenn erfolgreich, False bei Fehler.
    """
    import time as _time

    try:
        pdf_path = Path(pdf_path)
        out = pdf_path.with_suffix(".extracted.json")
        if out.exists():
            return True

        full_text = pdf_analyzer.extract_text_from_pdf(str(pdf_path), max_pages=None) or ""
        if not full_text:
            logger.warning(f"[{pdf_path.name}] Kein Text extrahierbar")
            return False

        chunks = _chunk_text(full_text)
        logger.info(f"[{pdf_path.name}] Extrahiere {len(chunks)} Chunk(s)")

        extracted: dict = {}
        for i, chunk in enumerate(chunks):
            t_start = _time.time()
            try:
                if provider.lower() == "gemini":
                    try:
                        from google import genai
                        from google.genai import types
                    except ImportError:
                        logger.error("google-genai nicht installiert")
                        return False
                    client = genai.Client(api_key=api_key)
                    resp = client.models.generate_content(
                        model=model,
                        contents=chunk,
                        config=types.GenerateContentConfig(
                            system_instruction=_EXTRACT_PROMPT,
                            max_output_tokens=4096,
                        ),
                    )
                    raw = resp.text if hasattr(resp, "text") else ""
                elif provider.lower() == "openrouter":
                    try:
                        from openai import OpenAI
                    except ImportError:
                        logger.error("openai nicht installiert")
                        return False
                    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
                    resp = client.chat.completions.create(
                        model=model,
                        max_tokens=4096,
                        messages=[
                            {"role": "system", "content": _EXTRACT_PROMPT},
                            {"role": "user", "content": chunk},
                        ],
                    )
                    raw = (resp.choices[0].message.content or "") if resp.choices else ""
                else:  # anthropic
                    client = anthropic.Anthropic(api_key=api_key)
                    resp = client.messages.create(
                        model=model,
                        max_tokens=4096,
                        system=[{"type": "text", "text": _EXTRACT_PROMPT,
                                 "cache_control": {"type": "ephemeral"}}],
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": chunk,
                             "cache_control": {"type": "ephemeral"}},
                        ]}],
                    )
                    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))

                # Parse JSON
                raw = raw.strip()
                for marker in ("```json", "```"):
                    if marker in raw:
                        raw = raw.split(marker)[1].split("```")[0].strip()
                        break
                start, end = raw.find("{"), raw.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        part = json.loads(raw[start:end])
                        extracted = _merge_extracted(extracted, part)
                    except json.JSONDecodeError:
                        logger.warning(f"[{pdf_path.name}] Chunk {i+1}: JSON-Parse fehlgeschlagen")
            except Exception as exc:
                logger.error(f"[{pdf_path.name}] Chunk {i+1} Fehler: {exc}")
                return False

            if len(chunks) > 1 and i < len(chunks) - 1:
                wait = max(0.0, _CHUNK_MIN_INTERVAL - (_time.time() - t_start))
                if wait > 0:
                    _time.sleep(wait)

        # Speichern
        out.write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[{pdf_path.name}] Extraktion fertig → {out.name}")
        return True
    except Exception as exc:
        logger.error(f"Auto-Extraktion Error: {exc}")
        return False


# ─── Worker ──────────────────────────────────────────────────────────────────

class _TrimWorker(threading.Thread):
    """Extrahiert Tabellen und kürzt PDF-Text per LLM in einem eigenen Thread."""

    def __init__(
        self,
        pdf_path: str,
        prompt: str,
        model: str,
        api_key: str,
        result_queue: queue.Queue,
        provider: str = "anthropic",
    ):
        super().__init__(daemon=True)
        self._pdf_path = pdf_path
        self._prompt = prompt
        self._model = model
        self._api_key = api_key
        self._provider = provider.lower()
        self._queue = result_queue
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _call_llm(self, text: str) -> str:
        if self._provider == "gemini":
            return self._call_llm_gemini(text)
        if self._provider == "openrouter":
            return self._call_llm_openrouter(text)
        return self._call_llm_anthropic(text)

    def _call_llm_anthropic(self, text: str) -> str:
        client = anthropic.Anthropic(api_key=self._api_key)
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=8192,
                system=[{"type": "text", "text": self._prompt,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": text[:80_000],
                     "cache_control": {"type": "ephemeral"}},
                ]}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Ungültiger API-Key — bitte im Admin konfigurieren.")
        except anthropic.RateLimitError:
            raise RuntimeError("Rate Limit erreicht — bitte kurz warten und erneut starten.")
        except anthropic.BadRequestError as exc:
            raise RuntimeError(f"Eingabe zu lang für Modell-Kontext: {exc}")
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"API-Fehler {exc.status_code}: {exc.message}")
        if response.stop_reason == "max_tokens":
            self._queue.put(("log", "⚠ Ausgabe-Limit (max_tokens) — Text möglicherweise abgeschnitten."))
        return "".join(b.text for b in response.content if hasattr(b, "text"))

    def _call_llm_gemini(self, text: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError("google-generativeai nicht installiert. Bitte: pip install google-generativeai")
        genai.configure(api_key=self._api_key)
        model = genai.GenerativeModel(self._model, system_instruction=self._prompt)
        try:
            response = model.generate_content(
                text[:80_000],
                generation_config={"max_output_tokens": 8192},
            )
        except Exception as exc:
            msg = str(exc)
            if "API_KEY_INVALID" in msg or "UNAUTHENTICATED" in msg:
                raise RuntimeError("Ungültiger Gemini API-Key — bitte im Admin konfigurieren.")
            raise RuntimeError(f"Gemini API-Fehler: {exc}")
        return response.text if hasattr(response, "text") else ""

    def _call_llm_openrouter(self, text: str) -> str:
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
                    {"role": "system", "content": self._prompt},
                    {"role": "user",   "content": text[:80_000]},
                ],
            )
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "invalid_api_key" in msg.lower() or "authentication" in msg.lower():
                raise RuntimeError("Ungültiger OpenRouter API-Key — bitte im Admin konfigurieren.")
            raise RuntimeError(f"OpenRouter API-Fehler: {exc}")
        return (response.choices[0].message.content or "") if response.choices else ""

    def run(self):
        try:
            # 1. Tabellen extrahieren (schnell, synchron)
            self._queue.put(("log", "Tabellen extrahieren …"))
            tables = pdf_analyzer.extract_tables_from_pdf(self._pdf_path, max_pages=None)
            pdf_analyzer.save_tables_json(self._pdf_path, tables)
            self._queue.put(("tables", tables))
            self._queue.put(("log", f"{len(tables)} Tabelle(n) gefunden und gespeichert"))

            if self._stop_flag:
                self._queue.put(("done", None))
                return

            # 2. Volltext extrahieren (für LLM-Trim immer Volltext, nicht gefiltert)
            self._queue.put(("log", "Volltext extrahieren …"))
            full_text = pdf_analyzer.extract_text_from_pdf(self._pdf_path, max_pages=None) or ""
            if not full_text:
                self._queue.put(("error", "Kein Text aus PDF extrahierbar"))
                self._queue.put(("done", None))
                return
            orig_len = len(full_text)
            chunks = _chunk_text(full_text)
            if len(chunks) > 1:
                self._queue.put(("log",
                    f"{orig_len:,} Zeichen → {len(chunks)} Chunks à ≤{_CHUNK_MAX_CHARS:,} Zeichen"))
            else:
                self._queue.put(("log", f"{orig_len:,} Zeichen → LLM kürzt …"))

            if self._stop_flag:
                self._queue.put(("done", None))
                return

            # 3. LLM-Trim (chunk-weise)
            import time as _time
            trimmed_parts = []
            for i, chunk in enumerate(chunks):
                if self._stop_flag:
                    self._queue.put(("done", None))
                    return
                if len(chunks) > 1:
                    self._queue.put(("log",
                        f"  Chunk {i + 1}/{len(chunks)} ({len(chunk):,} Zeichen) …"))
                t_start = _time.time()
                try:
                    part = self._call_llm(chunk)
                except Exception as exc:
                    self._queue.put(("error", str(exc)))
                    self._queue.put(("done", None))
                    return
                trimmed_parts.append(part)
                if len(chunks) > 1 and i < len(chunks) - 1:
                    wait = max(0.0, _CHUNK_MIN_INTERVAL - (_time.time() - t_start))
                    if wait > 0:
                        self._queue.put(("log", f"  Rate-Limit-Pause {wait:.0f}s …"))
                        _time.sleep(wait)

            trimmed = "\n\n".join(trimmed_parts)
            reduction = 100 - int(len(trimmed) / max(orig_len, 1) * 100)
            self._queue.put(("trimmed", trimmed))
            self._queue.put(("log",
                f"Fertig: {orig_len:,} → {len(trimmed):,} Zeichen (−{reduction}%)"))

        except Exception as exc:
            self._queue.put(("error", str(exc)))
        finally:
            self._queue.put(("done", None))


# ─── Batch-Worker ────────────────────────────────────────────────────────────

class _BatchTrimWorker(threading.Thread):
    """Kürzt alle übergebenen PDFs parallel per LLM und speichert auto."""

    def __init__(
        self,
        pdf_paths: list[str],
        prompt: str,
        model: str,
        api_key: str,
        result_queue: queue.Queue,
        workers: int = 2,
        provider: str = "anthropic",
    ):
        super().__init__(daemon=True)
        self._pdf_paths = pdf_paths
        self._prompt = prompt
        self._model = model
        self._api_key = api_key
        self._provider = provider.lower()
        self._queue = result_queue
        self._stop_flag = False
        self._workers = max(1, workers)
        self._done = 0
        self._lock = threading.Lock()

    def _call_llm(self, text: str) -> str:
        if self._provider == "gemini":
            return self._call_llm_gemini(text)
        if self._provider == "openrouter":
            return self._call_llm_openrouter(text)
        return self._call_llm_anthropic(text)

    def _call_llm_anthropic(self, text: str) -> str:
        client = anthropic.Anthropic(api_key=self._api_key)
        response = client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=[{"type": "text", "text": self._prompt,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": text[:80_000],
                 "cache_control": {"type": "ephemeral"}},
            ]}],
        )
        return "".join(b.text for b in response.content if hasattr(b, "text"))

    def _call_llm_gemini(self, text: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError("google-generativeai nicht installiert.")
        genai.configure(api_key=self._api_key)
        model = genai.GenerativeModel(self._model, system_instruction=self._prompt)
        response = model.generate_content(
            text[:80_000],
            generation_config={"max_output_tokens": 8192},
        )
        return response.text if hasattr(response, "text") else ""

    def _call_llm_openrouter(self, text: str) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai nicht installiert. Bitte: pip install openai")
        client = OpenAI(api_key=self._api_key, base_url="https://openrouter.ai/api/v1")
        response = client.chat.completions.create(
            model=self._model,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": self._prompt},
                {"role": "user",   "content": text[:80_000]},
            ],
        )
        return (response.choices[0].message.content or "") if response.choices else ""

    def stop(self):
        self._stop_flag = True

    def _emit(self, evt_type: str, payload):
        self._queue.put((evt_type, payload))

    def _process_one(self, pdf_path: str, total: int):
        if self._stop_flag:
            return
        name = Path(pdf_path).name
        try:
            tables = pdf_analyzer.extract_tables_from_pdf(pdf_path, max_pages=None)
            pdf_analyzer.save_tables_json(pdf_path, tables)

            full_text = pdf_analyzer.extract_text_from_pdf(pdf_path, max_pages=None) or ""
            if not full_text:
                self._emit("log", f"  ⚠ {name}: Kein Text extrahierbar — übersprungen")
                with self._lock:
                    done = self._done
                self._emit("batch_progress", (done, total, pdf_path))
                return

            chunks = _chunk_text(full_text)
            if len(chunks) > 1:
                self._emit("log",
                    f"  {name}: {len(full_text):,} Zeichen → {len(chunks)} Chunks …")
            import time as _time
            trimmed_parts = []
            for i, chunk in enumerate(chunks):
                if self._stop_flag:
                    return
                t_start = _time.time()
                try:
                    part = self._call_llm(chunk)
                except Exception as exc:
                    msg = str(exc)
                    if "API-Key" in msg or "Ungültig" in msg:
                        self._emit("log", f"✗ {name}: {msg} — Batch abgebrochen.")
                        self._stop_flag = True
                        return
                    self._emit("log", f"  ✗ {name} Chunk {i + 1}: {msg}")
                    with self._lock:
                        done = self._done
                    self._emit("batch_progress", (done, total, pdf_path))
                    return
                trimmed_parts.append(part)
                if len(chunks) > 1 and i < len(chunks) - 1:
                    wait = max(0.0, _CHUNK_MIN_INTERVAL - (_time.time() - t_start))
                    if wait > 0:
                        _time.sleep(wait)

            trimmed = "\n\n".join(trimmed_parts)
            out = Path(pdf_path).with_suffix(".trimmed.txt")
            out.write_text(trimmed, encoding="utf-8")

            reduction = 100 - int(len(trimmed) / max(len(full_text), 1) * 100)
            with self._lock:
                self._done += 1
                done = self._done
            self._emit("log", f"  ✓ {name}: {len(full_text):,} → {len(trimmed):,} Zeichen (−{reduction}%)")

        except Exception as exc:
            self._emit("log", f"  ✗ {name}: Fehler: {exc}")
            with self._lock:
                done = self._done

        self._emit("batch_progress", (done, total, pdf_path))

    def run(self):
        total = len(self._pdf_paths)
        self._emit("log", f"Batch gestartet: {total} PDF(s), {self._workers} Worker …")

        with ThreadPoolExecutor(max_workers=self._workers) as executor:
            futures = {
                executor.submit(self._process_one, pdf_path, total): pdf_path
                for pdf_path in self._pdf_paths
            }
            for future in as_completed(futures):
                if self._stop_flag:
                    for f in futures:
                        f.cancel()
                try:
                    future.result()
                except Exception as exc:
                    self._emit("log", f"  ✗ Unerwarteter Fehler: {exc}")

        self._emit("batch_done", self._done)


# ─── Extractions-Worker ───────────────────────────────────────────────────────

class _ExtractWorker(threading.Thread):
    """Extrahiert Fondsdaten chunk-weise als JSON und speichert .extracted.json."""

    def __init__(
        self,
        pdf_path: str,
        model: str,
        api_key: str,
        result_queue: queue.Queue,
        provider: str = "anthropic",
    ):
        super().__init__(daemon=True)
        self._pdf_path   = pdf_path
        self._model      = model
        self._api_key    = api_key
        self._provider   = provider.lower()
        self._queue      = result_queue
        self._stop_flag  = False

    def stop(self):
        self._stop_flag = True

    def _call_llm(self, chunk: str) -> dict:
        if self._provider == "gemini":
            return self._call_gemini(chunk)
        if self._provider == "openrouter":
            return self._call_openrouter(chunk)
        return self._call_anthropic(chunk)

    def _parse_json(self, raw: str) -> dict:
        raw = raw.strip()
        for marker in ("```json", "```"):
            if marker in raw:
                raw = raw.split(marker)[1].split("```")[0].strip()
                break
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start < 0 or end <= start:
            return {}
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            try:
                from json_repair import repair_json
                result = repair_json(raw[start:end], return_objects=True)
                return result if isinstance(result, dict) else {}
            except Exception:
                return {}

    def _call_anthropic(self, chunk: str) -> dict:
        client = anthropic.Anthropic(api_key=self._api_key)
        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=[{"type": "text", "text": _EXTRACT_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": chunk,
                     "cache_control": {"type": "ephemeral"}},
                ]}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Ungültiger API-Key — bitte im Admin konfigurieren.")
        except anthropic.RateLimitError:
            raise RuntimeError("Rate Limit erreicht — bitte kurz warten.")
        except anthropic.BadRequestError as exc:
            raise RuntimeError(f"Eingabe zu lang: {exc}")
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"API-Fehler {exc.status_code}: {exc.message}")
        raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
        return self._parse_json(raw)

    def _call_gemini(self, chunk: str) -> dict:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise RuntimeError("google-genai nicht installiert.")
        client = genai.Client(api_key=self._api_key)
        try:
            resp = client.models.generate_content(
                model=self._model,
                contents=chunk,
                config=types.GenerateContentConfig(
                    system_instruction=_EXTRACT_PROMPT,
                    max_output_tokens=4096,
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Gemini Fehler: {exc}")
        return self._parse_json(resp.text if hasattr(resp, "text") else "")

    def _call_openrouter(self, chunk: str) -> dict:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai nicht installiert.")
        client = OpenAI(api_key=self._api_key, base_url="https://openrouter.ai/api/v1")
        try:
            resp = client.chat.completions.create(
                model=self._model,
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": _EXTRACT_PROMPT},
                    {"role": "user",   "content": chunk},
                ],
            )
        except Exception as exc:
            raise RuntimeError(f"OpenRouter Fehler: {exc}")
        raw = (resp.choices[0].message.content or "") if resp.choices else ""
        return self._parse_json(raw)

    def run(self):
        import time as _time
        try:
            self._queue.put(("log", "Volltext extrahieren (alle Seiten) …"))
            full_text = pdf_analyzer.extract_text_from_pdf(self._pdf_path, max_pages=None) or ""
            if not full_text:
                self._queue.put(("error", "Kein Text aus PDF extrahierbar"))
                self._queue.put(("done", None))
                return

            chunks = _chunk_text(full_text)
            self._queue.put(("log",
                f"{len(full_text):,} Zeichen → {len(chunks)} Chunk(s) à ≤{_CHUNK_MAX_CHARS:,}"))

            extracted: dict = {}
            for i, chunk in enumerate(chunks):
                if self._stop_flag:
                    self._queue.put(("done", None))
                    return
                self._queue.put(("log",
                    f"  Chunk {i + 1}/{len(chunks)} ({len(chunk):,} Zeichen) extrahieren …"))
                t_start = _time.time()
                try:
                    part = self._call_llm(chunk)
                except Exception as exc:
                    self._queue.put(("error", str(exc)))
                    self._queue.put(("done", None))
                    return
                if part:
                    extracted = _merge_extracted(extracted, part)
                    n_pf  = len(extracted.get("portfolios") or [])
                    n_kl  = sum(
                        len(pf.get("anteilsklassen") or [])
                        for pf in (extracted.get("portfolios") or [])
                    )
                    self._queue.put(("log",
                        f"    → {n_pf} Portfolio(s), {n_kl} Anteilsklasse(n) bisher"))
                else:
                    self._queue.put(("log", "    → keine relevanten Infos gefunden"))

                if len(chunks) > 1 and i < len(chunks) - 1:
                    wait = max(0.0, _CHUNK_MIN_INTERVAL - (_time.time() - t_start))
                    if wait > 0:
                        self._queue.put(("log", f"  Rate-Limit-Pause {wait:.0f}s …"))
                        _time.sleep(wait)

            # Speichern
            out = Path(self._pdf_path).with_suffix(".extracted.json")
            out.write_text(
                json.dumps(extracted, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            n_pf = len(extracted.get("portfolios") or [])
            n_kl = sum(
                len(pf.get("anteilsklassen") or [])
                for pf in (extracted.get("portfolios") or [])
            )
            self._queue.put(("extracted", extracted))
            self._queue.put(("log",
                f"Fertig: {n_pf} Portfolio(s), {n_kl} Anteilsklasse(n) → {out.name}"))
        except Exception as exc:
            self._queue.put(("error", str(exc)))
        finally:
            self._queue.put(("done", None))


# ─── Fenster ─────────────────────────────────────────────────────────────────

class PdfTrimWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("PDF-Kürzer")
        self.configure(bg=BG_MAIN)
        self.geometry("1120x760")
        self.minsize(820, 560)

        self._pdf_folder = _PDF_FOLDER
        self._worker: _TrimWorker | _BatchTrimWorker | None = None
        self._event_queue: queue.Queue = queue.Queue()
        self._pdf_list_data: list[dict] = []
        self._search_var = tk.StringVar()
        self._pending_trimmed: str = ""
        self._selected_pdf: str = ""
        self._isin_map: dict[str, list[str]] = {}

        self._build_ui()
        self._load_isin_map()
        self._refresh_list()
        self._poll_queue()

    # ─── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Toolbar
        toolbar = tk.Frame(self, bg=BG_PANEL)
        toolbar.pack(fill="x")
        inner = tk.Frame(toolbar, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        tk.Label(inner, text="✂  PDF-Kürzer",
                 bg=BG_PANEL, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        self._btn_stop = tk.Button(
            inner, text="⏹  Stopp", command=self._stop_worker,
            bg="#3a2000", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE, state="disabled")
        self._btn_stop.pack(side="right", padx=(4, 0))

        self._btn_batch = tk.Button(
            inner, text="📦  Alle trimmen", command=self._start_batch_worker,
            bg="#1e2a3e", fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE)
        self._btn_batch.pack(side="right", padx=(4, 0))

        self._btn_extract = tk.Button(
            inner, text="🔍  Extrahieren", command=self._start_extract_worker,
            bg="#1e1a2e", fg=ACCENT_LAVENDER, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE)
        self._btn_extract.pack(side="right", padx=(4, 0))

        self._btn_run = tk.Button(
            inner, text="✂  Trimmen", command=self._start_worker,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE)
        self._btn_run.pack(side="right", padx=(4, 0))

        self._auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            inner, text="Auto-speichern",
            variable=self._auto_var,
            bg=BG_PANEL, fg=FG_MUTED, selectcolor=BG_INPUT,
            activebackground=BG_PANEL, activeforeground=FG_TEXT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="right", padx=(12, 4))

        self._model_var = tk.StringVar(value=_MODELS[0])
        self._model_combo = ttk.Combobox(
            inner, textvariable=self._model_var, values=_MODELS,
            state="readonly", width=28, font=("Segoe UI", 9),
        )
        self._model_combo.pack(side="right", padx=(2, 0))
        tk.Label(inner, text="Modell:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(8, 2))

        import os
        self._provider_var = tk.StringVar(value=os.getenv("LLM_PROVIDER", "anthropic"))
        provider_combo = ttk.Combobox(
            inner, textvariable=self._provider_var,
            values=["anthropic", "gemini", "openrouter"], state="readonly", width=12,
            font=("Segoe UI", 9),
        )
        provider_combo.pack(side="right", padx=(2, 0))
        provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)
        tk.Label(inner, text="Anbieter:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(8, 2))

        self._workers_var = tk.IntVar(value=2)
        tk.Spinbox(
            inner, from_=1, to=4, textvariable=self._workers_var,
            width=3, bg=BG_INPUT, fg=FG_TEXT, font=("Segoe UI", 9),
            buttonbackground=BTN_BG, relief="flat",
        ).pack(side="right", padx=(2, 0))
        tk.Label(inner, text="Worker:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(8, 2))

        # Hauptbereich
        main = tk.Frame(self, bg=BG_MAIN)
        main.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        # ── Linke Spalte: PDF-Liste ──────────────────────────────────────────
        left = tk.Frame(main, bg=BG_MAIN, width=300)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)

        tk.Label(left, text="PDFs in data/prospekte/",
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))

        # Suchfeld
        search_row = tk.Frame(left, bg=BG_MAIN)
        search_row.pack(fill="x", pady=(0, 4))
        tk.Label(search_row, text="🔍", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 2))
        self._search_var.trace_add("write", lambda *_: self._fill_list())
        tk.Entry(search_row, textvariable=self._search_var,
                 bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                 relief="flat", font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True)
        tk.Button(search_row, text="✕",
                  command=lambda: self._search_var.set(""),
                  bg=BG_MAIN, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 8), padx=2, pady=0,
                  cursor="hand2", activebackground=BG_MAIN).pack(side="left", padx=(2, 0))

        list_frame = tk.Frame(left, bg=BG_MAIN)
        list_frame.pack(fill="both", expand=True)

        self._tree = ttk.Treeview(
            list_frame,
            columns=("name", "trim", "ext", "tab", "isins"),
            show="headings", selectmode="browse",
        )
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=BG_PANEL, foreground=FG_TEXT,
                        fieldbackground=BG_PANEL, rowheight=22,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=BG_INPUT,
                        foreground=ACCENT_BLUE, font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3a3a5e")])

        self._tree.heading("name",  text="Datei")
        self._tree.heading("trim",  text="Trim")
        self._tree.heading("ext",   text="Ext.")
        self._tree.heading("tab",   text="Tab.")
        self._tree.heading("isins", text="ISINs")
        self._tree.column("name",  width=145, anchor="w")
        self._tree.column("trim",  width=34,  anchor="center")
        self._tree.column("ext",   width=34,  anchor="center")
        self._tree.column("tab",   width=34,  anchor="center")
        self._tree.column("isins", width=34,  anchor="center")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

        self._tree.tag_configure("trimmed", foreground=ACCENT_GREEN)
        self._tree.tag_configure("plain",   foreground=FG_TEXT)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<Double-1>", self._on_double_click)

        tk.Button(
            left, text="↺  Aktualisieren", command=self._refresh_list,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(pady=(4, 0), anchor="w")

        # ── Rechte Spalte ────────────────────────────────────────────────────
        right = tk.Frame(main, bg=BG_MAIN)
        right.pack(side="left", fill="both", expand=True)

        # Fixe Elemente zuerst von unten verankern, damit expand=True der
        # Vorschau sie nicht aus dem sichtbaren Bereich drängt.
        bottom = tk.Frame(right, bg=BG_MAIN)
        bottom.pack(side="bottom", fill="x")

        # Vorschau — expandiert in den verbleibenden Platz (nach bottom packen!)
        tk.Label(right, text="Vorschau / Ergebnis",
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))
        self._preview = scrolledtext.ScrolledText(
            right, height=14, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat", wrap="word")
        self._preview.pack(fill="both", expand=True)

        # Log
        tk.Label(bottom, text="Log", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))
        self._log = scrolledtext.ScrolledText(
            bottom, height=4, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat")
        self._log.pack(fill="x", pady=(0, 8))

        # Prompt-Bereich
        tk.Label(bottom, text="Trim-Prompt (editierbar)",
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(6, 2))
        self._prompt_box = scrolledtext.ScrolledText(
            bottom, height=6, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), insertbackground=FG_TEXT,
            relief="flat", wrap="word")
        self._prompt_box.pack(fill="x")
        self._prompt_box.insert("1.0", _DEFAULT_PROMPT)

        # Speichern/Verwerfen (erscheint nach LLM-Ergebnis ohne Auto-speichern)
        self._confirm_frame = tk.Frame(bottom, bg=BG_MAIN)
        tk.Button(
            self._confirm_frame, text="✔  Speichern",
            command=self._save_trimmed,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            self._confirm_frame, text="✖  Verwerfen",
            command=self._discard_trimmed,
            bg="#2e1a1a", fg=ACCENT_RED, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left")

        # Tabellen-Info-Zeile
        self._tab_label = tk.Label(
            bottom, text="Tabellen: —",
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8), anchor="w")
        self._tab_label.pack(fill="x", pady=(2, 0))

    # ─── Daten ───────────────────────────────────────────────────────────────

    def _load_isin_map(self):
        self._isin_map = {}
        try:
            for r in results_store.get_all_results():
                pfad = r.get("prospekt_pfad", "") or ""
                if pfad:
                    self._isin_map.setdefault(Path(pfad).name, []).append(r["isin"])
        except Exception:
            pass

    def _on_provider_changed(self, _event=None):
        from llm_provider import get_models, DEFAULT_BATCH_MODELS
        provider = self._provider_var.get()
        models = get_models(provider)
        self._model_combo.config(values=models)
        # Trim verwendet günstiges Modell (Haiku/Flash)
        self._model_var.set(DEFAULT_BATCH_MODELS.get(provider, models[0]) if models else "")

    def _refresh_list(self):
        self._load_isin_map()
        self._pdf_list_data = []
        if self._pdf_folder.exists():
            for pdf in sorted(self._pdf_folder.glob("*.pdf")):
                has_trim = pdf.with_suffix(".trimmed.txt").exists()
                tables_file = pdf.with_suffix(".tables.json")
                tab_count = "—"
                if tables_file.exists():
                    try:
                        tab_count = str(len(json.loads(
                            tables_file.read_text(encoding="utf-8"))))
                    except Exception:
                        pass
                has_ext = pdf.with_suffix(".extracted.json").exists()
                isins = self._isin_map.get(pdf.name, [])
                self._pdf_list_data.append({
                    "path": str(pdf),
                    "name": pdf.name,
                    "trim": "✓" if has_trim else "—",
                    "ext":  "✓" if has_ext  else "—",
                    "tab":  tab_count,
                    "isins": str(len(isins)) if isins else "—",
                    "tag":  "trimmed" if (has_trim or has_ext) else "plain",
                })
        self._fill_list()

    def _fill_list(self):
        search = self._search_var.get().strip().lower()
        self._tree.delete(*self._tree.get_children())
        for item in self._pdf_list_data:
            if search and search not in item["name"].lower():
                continue
            self._tree.insert("", "end", iid=item["path"], values=(
                item["name"], item["trim"], item["ext"], item["tab"], item["isins"],
            ), tags=(item["tag"],))

    # ─── Auswahl ─────────────────────────────────────────────────────────────

    def _on_select(self, _event=None):
        sel = self._tree.selection()
        if not sel:
            return
        pdf_path = sel[0]
        self._selected_pdf = pdf_path
        self._pending_trimmed = ""
        self._confirm_frame.pack_forget()

        extracted_file = Path(pdf_path).with_suffix(".extracted.json")
        trimmed_file   = Path(pdf_path).with_suffix(".trimmed.txt")
        if extracted_file.exists():
            try:
                data = json.loads(extracted_file.read_text(encoding="utf-8"))
                n_pf = len(data.get("portfolios") or [])
                n_kl = sum(len(pf.get("anteilsklassen") or [])
                           for pf in (data.get("portfolios") or []))
                preview = (f"[Extracted JSON — {n_pf} Portfolio(s), {n_kl} Anteilsklasse(n)]\n\n"
                           + json.dumps(data, ensure_ascii=False, indent=2))
                self._set_preview(preview)
            except Exception:
                self._set_preview("[Extracted JSON — Lesefehler]")
        elif trimmed_file.exists():
            text = trimmed_file.read_text(encoding="utf-8")
            self._set_preview(f"[Trimmed-Text — {len(text):,} Zeichen]\n\n{text}")
        else:
            self._set_preview("[Volltext wird geladen …]")
            threading.Thread(
                target=self._load_preview_bg, args=(pdf_path,), daemon=True
            ).start()

        tables_file = Path(pdf_path).with_suffix(".tables.json")
        if tables_file.exists():
            try:
                self._show_table_info(json.loads(
                    tables_file.read_text(encoding="utf-8")))
            except Exception:
                self._tab_label.config(text="Tabellen: (Lesefehler)")
        else:
            self._tab_label.config(text="Tabellen: noch nicht extrahiert")

    def _load_preview_bg(self, pdf_path: str):
        meta = pdf_analyzer.get_pdf_metadata(pdf_path)
        size_mb = Path(pdf_path).stat().st_size / 1_048_576
        pages = meta.get("pages", "?")
        title = meta.get("title") or ""
        lines = [
            f"Datei:  {Path(pdf_path).name}",
            f"Größe:  {size_mb:.1f} MB",
            f"Seiten: {pages}",
        ]
        if title:
            lines.append(f"Titel:  {title}")
        lines.append("")
        lines.append("Volltext erst beim ▶ Verarbeiten extrahiert.")
        self._set_preview("\n".join(lines))

    def _set_preview(self, text: str):
        self._preview.config(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", text)
        self._preview.config(state="disabled")

    def _show_table_info(self, tables: list[dict]):
        if not tables:
            self._tab_label.config(text="Tabellen: keine gefunden")
            return
        parts = []
        for t in tables[:4]:
            cols = " | ".join(h for h in t.get("headers", [])[:4] if h)
            parts.append(f"S.{t['page']}: [{cols}] ({len(t.get('rows', []))} Zeilen)")
        suffix = f" +{len(tables) - 4} weitere" if len(tables) > 4 else ""
        self._tab_label.config(text="Tabellen: " + "  ·  ".join(parts) + suffix)

    def _on_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item:
            return
        pdf = Path(item)
        menu = tk.Menu(self, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                       activebackground=BTN_ACTIVE, activeforeground=FG_TEXT)

        def _del(suffix: str):
            f = pdf.with_suffix(suffix)
            if f.exists():
                f.unlink()
                self._log_line(f"Gelöscht: {f.name}")
                self._refresh_list()
                if self._selected_pdf == str(pdf):
                    self._on_select()

        menu.add_command(label="Extracted löschen (.extracted.json)",
                         command=lambda: _del(".extracted.json"))
        menu.add_command(label="Trimmed löschen (.trimmed.txt)",
                         command=lambda: _del(".trimmed.txt"))
        menu.add_command(label="Tabellen löschen (.tables.json)",
                         command=lambda: _del(".tables.json"))
        menu.tk_popup(event.x_root, event.y_root)

    def _on_double_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        self._tree.selection_set(iid)
        self._on_select()
        self._start_worker()

    # ─── Worker ──────────────────────────────────────────────────────────────

    def _start_worker(self):
        if not self._selected_pdf:
            messagebox.showwarning("Kein PDF", "Bitte zuerst ein PDF auswählen.",
                                   parent=self)
            return
        if self._worker and self._worker.is_alive():
            return

        provider = self._provider_var.get()
        _key_env = {"gemini": "GOOGLE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(
            provider, "ANTHROPIC_API_KEY"
        )
        api_key = os.getenv(_key_env, "")
        if not api_key:
            messagebox.showerror("API-Key fehlt",
                                 f"Kein {_key_env} in .env gefunden.",
                                 parent=self)
            return

        self._pending_trimmed = ""
        self._confirm_frame.pack_forget()
        self._set_preview("[Verarbeitung läuft …]")
        self._tab_label.config(text="Tabellen: wird extrahiert …")

        self._event_queue = queue.Queue()
        self._worker = _TrimWorker(
            pdf_path=self._selected_pdf,
            prompt=self._prompt_box.get("1.0", "end-1c").strip(),
            model=self._model_var.get(),
            api_key=api_key,
            result_queue=self._event_queue,
            provider=provider,
        )
        self._worker.start()
        self._set_running(True)

    def _start_batch_worker(self):
        if self._worker and self._worker.is_alive():
            return

        if not self._pdf_folder.exists():
            messagebox.showwarning("Kein Ordner",
                                   f"PDF-Ordner nicht gefunden:\n{self._pdf_folder}",
                                   parent=self)
            return

        provider = self._provider_var.get()
        _key_env = {"gemini": "GOOGLE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(
            provider, "ANTHROPIC_API_KEY"
        )
        api_key = os.getenv(_key_env, "")
        if not api_key:
            messagebox.showerror("API-Key fehlt",
                                 f"Kein {_key_env} in .env gefunden.",
                                 parent=self)
            return

        untrimmed = [
            str(p) for p in sorted(self._pdf_folder.glob("*.pdf"))
            if not p.with_suffix(".trimmed.txt").exists()
        ]

        if not untrimmed:
            messagebox.showinfo("Alles erledigt",
                                "Alle PDFs haben bereits eine .trimmed.txt-Datei.",
                                parent=self)
            return

        if not messagebox.askyesno(
            "Batch starten",
            f"{len(untrimmed)} PDF(s) ohne .trimmed.txt gefunden.\n\n"
            "Ergebnisse werden automatisch gespeichert.\nJetzt starten?",
            parent=self,
        ):
            return

        self._event_queue = queue.Queue()
        self._worker = _BatchTrimWorker(
            pdf_paths=untrimmed,
            prompt=self._prompt_box.get("1.0", "end-1c").strip(),
            model=self._model_var.get(),
            api_key=api_key,
            result_queue=self._event_queue,
            workers=self._workers_var.get(),
            provider=provider,
        )
        self._worker.start()
        self._set_running(True)

    def _start_extract_worker(self):
        if not self._selected_pdf:
            messagebox.showwarning("Kein PDF", "Bitte zuerst ein PDF auswählen.",
                                   parent=self)
            return
        if self._worker and self._worker.is_alive():
            return

        provider = self._provider_var.get()
        _key_env = {"gemini": "GOOGLE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(
            provider, "ANTHROPIC_API_KEY"
        )
        api_key = os.getenv(_key_env, "")
        if not api_key:
            messagebox.showerror("API-Key fehlt",
                                 f"Kein {_key_env} in .env gefunden.",
                                 parent=self)
            return

        self._set_preview("[Extraktion läuft …]")
        self._event_queue = queue.Queue()
        self._worker = _ExtractWorker(
            pdf_path=self._selected_pdf,
            model=self._model_var.get(),
            api_key=api_key,
            result_queue=self._event_queue,
            provider=provider,
        )
        self._worker.start()
        self._set_running(True)

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self._btn_run.config(state=state)
        self._btn_extract.config(state=state)
        self._btn_batch.config(state=state)
        self._btn_stop.config(state="normal" if running else "disabled")
        if hasattr(self.master, "notify_process"):
            self.master.notify_process("PDF-Kürzer", running)

    # ─── Queue-Polling ────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                evt_type, payload = self._event_queue.get_nowait()
                if evt_type == "log":
                    self._log_line(payload)
                elif evt_type == "error":
                    self._log_line(f"FEHLER: {payload}")
                elif evt_type == "tables":
                    self._show_table_info(payload)
                    self._refresh_list()
                elif evt_type == "trimmed":
                    self._handle_trimmed(payload)
                elif evt_type == "extracted":
                    self._on_select()   # Vorschau aus frisch gespeicherter .extracted.json laden
                elif evt_type == "done":
                    self._set_running(False)
                    self._refresh_list()
                elif evt_type == "batch_progress":
                    _done, _total, _path = payload
                    self._refresh_list()
                    # Select the just-processed PDF in the list to give visual feedback
                    if str(_path) in self._tree.get_children():
                        self._tree.see(str(_path))
                elif evt_type == "batch_done":
                    self._set_running(False)
                    self._refresh_list()
                    self._log_line(
                        f"✓ Batch abgeschlossen — {payload} PDF(s) erfolgreich verarbeitet"
                    )
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _handle_trimmed(self, text: str):
        if self._auto_var.get():
            self._write_trimmed(text)
            self._set_preview(f"[Auto-gespeichert — {len(text):,} Zeichen]\n\n{text}")
        else:
            self._pending_trimmed = text
            self._set_preview(
                f"[Vorschau — {len(text):,} Zeichen — noch nicht gespeichert]\n\n{text}")
            self._confirm_frame.pack(fill="x", pady=(4, 0))

    def _save_trimmed(self):
        if self._pending_trimmed and self._selected_pdf:
            self._write_trimmed(self._pending_trimmed)
            self._pending_trimmed = ""
            self._confirm_frame.pack_forget()

    def _discard_trimmed(self):
        self._pending_trimmed = ""
        self._confirm_frame.pack_forget()
        threading.Thread(
            target=self._load_preview_bg, args=(self._selected_pdf,), daemon=True
        ).start()

    def _write_trimmed(self, text: str):
        out = Path(self._selected_pdf).with_suffix(".trimmed.txt")
        out.write_text(text, encoding="utf-8")
        self._log_line(f"Gespeichert: {out.name} ({len(text):,} Zeichen)")
        self._refresh_list()

    # ─── Log ─────────────────────────────────────────────────────────────────

    def _log_line(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")
