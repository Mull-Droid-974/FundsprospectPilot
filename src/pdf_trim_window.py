"""
PDF-Trim-Maske — Intelligentes Kürzen von Fondsprospekten per LLM.

Liest alle PDFs aus data/prospekte/ und ermöglicht:
- Strukturierte Tabellenextraktion  (→ .tables.json neben dem PDF)
- LLM-gestütztes Kürzen            (→ .trimmed.txt neben dem PDF)

Die Analyse-Pipeline (llm_analysis_worker) bevorzugt .trimmed.txt
automatisch, und injiziert .tables.json als strukturierten Kontext.
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
from dotenv import load_dotenv

import pdf_analyzer
import results_store

load_dotenv()

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
            tables = pdf_analyzer.extract_tables_from_pdf(self._pdf_path)
            pdf_analyzer.save_tables_json(self._pdf_path, tables)
            self._queue.put(("tables", tables))
            self._queue.put(("log", f"{len(tables)} Tabelle(n) gefunden und gespeichert"))

            if self._stop_flag:
                self._queue.put(("done", None))
                return

            # 2. Volltext extrahieren (für LLM-Trim immer Volltext, nicht gefiltert)
            self._queue.put(("log", "Volltext extrahieren …"))
            full_text = pdf_analyzer.extract_text_from_pdf(self._pdf_path) or ""
            if not full_text:
                self._queue.put(("error", "Kein Text aus PDF extrahierbar"))
                self._queue.put(("done", None))
                return
            orig_len = len(full_text)
            self._queue.put(("log", f"{orig_len:,} Zeichen → LLM kürzt …"))

            if self._stop_flag:
                self._queue.put(("done", None))
                return

            # 3. LLM-Trim
            try:
                trimmed = self._call_llm(full_text)
            except Exception as exc:
                self._queue.put(("error", str(exc)))
                self._queue.put(("done", None))
                return
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
            tables = pdf_analyzer.extract_tables_from_pdf(pdf_path)
            pdf_analyzer.save_tables_json(pdf_path, tables)

            full_text = pdf_analyzer.extract_text_from_pdf(pdf_path) or ""
            if not full_text:
                self._emit("log", f"  ⚠ {name}: Kein Text extrahierbar — übersprungen")
                with self._lock:
                    done = self._done
                self._emit("batch_progress", (done, total, pdf_path))
                return

            try:
                trimmed = self._call_llm(full_text)
            except Exception as exc:
                msg = str(exc)
                if "API-Key" in msg or "Ungültig" in msg:
                    self._emit("log", f"✗ {name}: {msg} — Batch abgebrochen.")
                    self._stop_flag = True
                    return
                self._emit("log", f"  ✗ {name}: {msg}")
                with self._lock:
                    done = self._done
                self._emit("batch_progress", (done, total, pdf_path))
                return
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

        self._btn_run = tk.Button(
            inner, text="▶  Verarbeiten", command=self._start_worker,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE)
        self._btn_run.pack(side="right", padx=(4, 0))

        self._auto_var = tk.BooleanVar(value=False)
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

        self._provider_var = tk.StringVar(value="anthropic")
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
            columns=("name", "trim", "tab", "isins"),
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
        self._tree.heading("tab",   text="Tab.")
        self._tree.heading("isins", text="ISINs")
        self._tree.column("name",  width=165, anchor="w")
        self._tree.column("trim",  width=38,  anchor="center")
        self._tree.column("tab",   width=38,  anchor="center")
        self._tree.column("isins", width=38,  anchor="center")

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

        tk.Label(right, text="Vorschau / Ergebnis",
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))

        self._preview = scrolledtext.ScrolledText(
            right, height=14, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat", wrap="word")
        self._preview.pack(fill="both", expand=True)

        # Tabellen-Info-Zeile
        self._tab_label = tk.Label(
            right, text="Tabellen: —",
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8), anchor="w")
        self._tab_label.pack(fill="x", pady=(2, 0))

        # Speichern/Verwerfen (nur nach LLM-Ergebnis ohne Auto-speichern)
        self._confirm_frame = tk.Frame(right, bg=BG_MAIN)
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

        # Prompt-Bereich
        tk.Label(right, text="Trim-Prompt (editierbar)",
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(6, 2))

        self._prompt_box = scrolledtext.ScrolledText(
            right, height=7, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), insertbackground=FG_TEXT,
            relief="flat", wrap="word")
        self._prompt_box.pack(fill="x")
        self._prompt_box.insert("1.0", _DEFAULT_PROMPT)

        # Log
        tk.Label(right, text="Log", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))
        self._log = scrolledtext.ScrolledText(
            right, height=4, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat")
        self._log.pack(fill="x", pady=(0, 8))

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
        from llm_provider import get_models, get_default_batch_model
        provider = self._provider_var.get()
        models = get_models(provider)
        self._model_combo.config(values=models)
        self._model_var.set(get_default_batch_model(provider) if models else "")

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
                isins = self._isin_map.get(pdf.name, [])
                self._pdf_list_data.append({
                    "path": str(pdf),
                    "name": pdf.name,
                    "trim": "✓" if has_trim else "—",
                    "tab":  tab_count,
                    "isins": str(len(isins)) if isins else "—",
                    "tag":  "trimmed" if has_trim else "plain",
                })
        self._fill_list()

    def _fill_list(self):
        search = self._search_var.get().strip().lower()
        self._tree.delete(*self._tree.get_children())
        for item in self._pdf_list_data:
            if search and search not in item["name"].lower():
                continue
            self._tree.insert("", "end", iid=item["path"], values=(
                item["name"], item["trim"], item["tab"], item["isins"],
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

        trimmed_file = Path(pdf_path).with_suffix(".trimmed.txt")
        if trimmed_file.exists():
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

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self._btn_run.config(state=state)
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
