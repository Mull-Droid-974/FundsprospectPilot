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
    ):
        super().__init__(daemon=True)
        self._pdf_path = pdf_path
        self._prompt = prompt
        self._model = model
        self._api_key = api_key
        self._queue = result_queue
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

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
            client = anthropic.Anthropic(api_key=self._api_key)
            response = client.messages.create(
                model=self._model,
                max_tokens=8192,
                system=[{"type": "text", "text": self._prompt,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": full_text[:80_000],
                     "cache_control": {"type": "ephemeral"}},
                ]}],
            )
            trimmed = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
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
    """Kürzt alle übergebenen PDFs sequentiell per LLM und speichert auto."""

    def __init__(
        self,
        pdf_paths: list[str],
        prompt: str,
        model: str,
        api_key: str,
        result_queue: queue.Queue,
    ):
        super().__init__(daemon=True)
        self._pdf_paths = pdf_paths
        self._prompt = prompt
        self._model = model
        self._api_key = api_key
        self._queue = result_queue
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _emit(self, evt_type: str, payload):
        self._queue.put((evt_type, payload))

    def run(self):
        total = len(self._pdf_paths)
        done = 0

        self._emit("log", f"Batch gestartet: {total} PDF(s) ohne .trimmed.txt")

        client = anthropic.Anthropic(api_key=self._api_key)

        for pdf_path in self._pdf_paths:
            if self._stop_flag:
                self._emit("log", f"Abgebrochen nach {done}/{total}")
                break

            name = Path(pdf_path).name
            self._emit("log", f"[{done + 1}/{total}] {name}")

            try:
                # Tabellen extrahieren
                tables = pdf_analyzer.extract_tables_from_pdf(pdf_path)
                pdf_analyzer.save_tables_json(pdf_path, tables)

                # Volltext
                full_text = pdf_analyzer.extract_text_from_pdf(pdf_path) or ""
                if not full_text:
                    self._emit("log", "  ⚠ Kein Text extrahierbar — übersprungen")
                    self._emit("batch_progress", (done + 1, total, pdf_path))
                    continue

                # LLM
                response = client.messages.create(
                    model=self._model,
                    max_tokens=8192,
                    system=[{"type": "text", "text": self._prompt,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": full_text[:80_000],
                         "cache_control": {"type": "ephemeral"}},
                    ]}],
                )
                trimmed = "".join(
                    block.text for block in response.content if hasattr(block, "text")
                )

                # Auto-speichern
                out = Path(pdf_path).with_suffix(".trimmed.txt")
                out.write_text(trimmed, encoding="utf-8")

                reduction = 100 - int(len(trimmed) / max(len(full_text), 1) * 100)
                self._emit("log",
                    f"  ✓ {len(full_text):,} → {len(trimmed):,} Zeichen (−{reduction}%)")
                done += 1

            except Exception as exc:
                self._emit("log", f"  ✗ Fehler: {exc}")

            self._emit("batch_progress", (done, total, pdf_path))

        self._emit("batch_done", done)


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
        ttk.Combobox(
            inner, textvariable=self._model_var, values=_MODELS,
            state="readonly", width=28, font=("Segoe UI", 9),
        ).pack(side="right", padx=(2, 0))
        tk.Label(inner, text="Modell:", bg=BG_PANEL, fg=FG_MUTED,
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

    def _refresh_list(self):
        self._load_isin_map()
        self._tree.delete(*self._tree.get_children())
        if not self._pdf_folder.exists():
            return
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
            tag = "trimmed" if has_trim else "plain"
            self._tree.insert("", "end", iid=str(pdf), values=(
                pdf.name,
                "✓" if has_trim else "—",
                tab_count,
                str(len(isins)) if isins else "—",
            ), tags=(tag,))

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
        text = pdf_analyzer.extract_text_from_pdf(pdf_path) or "(Kein Text extrahierbar)"
        self._set_preview(f"[Original — {len(text):,} Zeichen]\n\n{text}")

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

    # ─── Worker ──────────────────────────────────────────────────────────────

    def _start_worker(self):
        if not self._selected_pdf:
            messagebox.showwarning("Kein PDF", "Bitte zuerst ein PDF auswählen.",
                                   parent=self)
            return
        if self._worker and self._worker.is_alive():
            return

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror("API-Key fehlt",
                                 "Kein ANTHROPIC_API_KEY in .env gefunden.",
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

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror("API-Key fehlt",
                                 "Kein ANTHROPIC_API_KEY in .env gefunden.",
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
        self._set_preview("[Verworfen — Original wird geladen …]")
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
