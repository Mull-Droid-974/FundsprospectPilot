"""
FundsprospectPilot – Tkinter Desktop-App
Hauptfenster mit Konfiguration, Batch-Analyse und Einzelne-PDF-Modus.
"""

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from dotenv import load_dotenv, set_key

# Projektpfad zu sys.path hinzufügen
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

# Farben
BG_MAIN = "#1e1e2e"
BG_PANEL = "#2a2a3e"
BG_INPUT = "#313145"
FG_TEXT = "#cdd6f4"
FG_MUTED = "#7f849c"
ACCENT_BLUE = "#89b4fa"
ACCENT_GREEN = "#a6e3a1"
ACCENT_RED = "#f38ba8"
ACCENT_YELLOW = "#f9e2af"
ACCENT_LAVENDER = "#b4befe"
BTN_BG = "#45475a"
BTN_ACTIVE = "#585b70"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FundsprospectPilot")
        self.geometry("900x700")
        self.minsize(800, 600)
        self.configure(bg=BG_MAIN)

        # State
        self._batch_thread = None
        self._processor = None
        self._progress_queue = queue.Queue()
        self._running = False
        self._batch_results: list[dict] = []
        self._batch_start_time: float | None = None
        self._results_win = None       # ResultsWindow-Referenz (lazy open)
        self._data_mgmt_win = None     # DataManagementWindow-Referenz (lazy open)
        self._download_win = None      # DownloadWindow-Referenz (lazy open)
        self._analysis_win = None      # ProspektAnalysisWindow-Referenz (lazy open)
        self._current_isin_var = tk.StringVar(value="—")
        self._current_step_var = tk.StringVar(value="Bereit")

        self._build_ui()
        self._load_settings()
        self._poll_queue()

    # ─── UI aufbauen ──────────────────────────────────────────────

    def _build_ui(self):
        # Titelzeile
        title_frame = tk.Frame(self, bg=BG_MAIN)
        title_frame.pack(fill="x", padx=20, pady=(15, 5))

        tk.Label(
            title_frame, text="🏦 FundsprospectPilot",
            bg=BG_MAIN, fg=ACCENT_LAVENDER,
            font=("Segoe UI", 16, "bold")
        ).pack(side="left")

        tk.Label(
            title_frame, text="Fondsprospekt-Klassifizierung",
            bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 10)
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            title_frame, text="🔬  Analyse",
            command=self._open_analysis_window,
            bg=BTN_BG, fg=ACCENT_LAVENDER, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            title_frame, text="📄  Prospekte",
            command=self._open_download_window,
            bg=BTN_BG, fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            title_frame, text="📋  Ergebnisse",
            command=self._open_results,
            bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            title_frame, text="🗃  Datenverwaltung",
            command=self._open_data_management,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            title_frame, text="⚙  Admin",
            command=self._open_admin,
            bg=BTN_BG, fg=ACCENT_LAVENDER, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).pack(side="right")

        # Hauptbereich: Links Konfiguration, Rechts Log
        main_frame = tk.Frame(self, bg=BG_MAIN)
        main_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # Linke Spalte
        left_frame = tk.Frame(main_frame, bg=BG_MAIN)
        left_frame.pack(side="left", fill="both", expand=False, padx=(0, 10))
        left_frame.configure(width=380)
        left_frame.pack_propagate(False)

        self._build_config_panel(left_frame)
        self._build_buttons(left_frame)
        self._build_single_pdf_panel(left_frame)

        # Rechte Spalte: Log
        right_frame = tk.Frame(main_frame, bg=BG_MAIN)
        right_frame.pack(side="left", fill="both", expand=True)

        self._build_log_panel(right_frame)

        # Statusbar
        self._build_statusbar()

    def _panel(self, parent, title: str) -> tk.Frame:
        """Erstellt ein Panel mit Titel."""
        outer = tk.Frame(parent, bg=BG_PANEL, bd=1, relief="flat")
        outer.pack(fill="x", pady=(0, 10))

        tk.Label(
            outer, text=title, bg=BG_PANEL, fg=ACCENT_BLUE,
            font=("Segoe UI", 10, "bold"), anchor="w", padx=12, pady=6
        ).pack(fill="x")

        ttk.Separator(outer, orient="horizontal").pack(fill="x", padx=10)

        inner = tk.Frame(outer, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)
        return inner

    def _row(self, parent, label: str, row: int):
        """Erstellt eine Beschriftung."""
        tk.Label(
            parent, text=label, bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 9), anchor="w"
        ).grid(row=row, column=0, sticky="w", pady=2)

    def _entry(self, parent, var: tk.StringVar, row: int, show: str = "") -> tk.Entry:
        """Erstellt ein Eingabefeld."""
        e = tk.Entry(
            parent, textvariable=var, bg=BG_INPUT, fg=FG_TEXT,
            insertbackground=FG_TEXT, font=("Segoe UI", 9),
            relief="flat", bd=4, show=show
        )
        e.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
        return e

    def _browse_btn(self, parent, row: int, command, text: str = "...") -> tk.Button:
        return tk.Button(
            parent, text=text, command=command,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 8), padx=6, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).grid(row=row, column=2, padx=(4, 0), pady=2)

    def _build_config_panel(self, parent):
        inner = self._panel(parent, "⚙ Konfiguration")
        inner.columnconfigure(1, weight=1)

        self.var_excel = tk.StringVar()
        self.var_pdf_folder = tk.StringVar()
        self.var_api_key = tk.StringVar()
        self.var_batch_size = tk.StringVar(value="200")

        rows = [
            ("Excel-Datei:", self.var_excel, True),
            ("PDF-Ordner:", self.var_pdf_folder, True),
            ("API-Key:", self.var_api_key, False),
            ("Batch-Grösse:", self.var_batch_size, False),
        ]

        for i, (label, var, has_browse) in enumerate(rows):
            self._row(inner, label, i)
            is_key = label == "API-Key:"
            self._entry(inner, var, i, show="*" if is_key else "")

            if has_browse:
                cmd = self._browse_excel if "Excel" in label else self._browse_folder
                tk.Button(
                    inner, text="...", command=cmd,
                    bg=BTN_BG, fg=FG_TEXT, relief="flat",
                    font=("Segoe UI", 8), padx=6, cursor="hand2",
                    activebackground=BTN_ACTIVE
                ).grid(row=i, column=2, padx=(4, 0), pady=2)

    def _build_buttons(self, parent):
        btn_frame = tk.Frame(parent, bg=BG_MAIN)
        btn_frame.pack(fill="x", pady=(0, 10))

        self.btn_start = self._big_btn(
            btn_frame, "▶  Analyse starten", ACCENT_GREEN, "#1e3a1e", self._start_batch
        )
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.btn_stop = self._big_btn(
            btn_frame, "⏸  Stopp", ACCENT_YELLOW, "#3a3000", self._stop_batch
        )
        self.btn_stop.pack(side="left", fill="x", expand=True)
        self.btn_stop.config(state="disabled")

    def _big_btn(self, parent, text: str, fg: str, bg: str, cmd) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            bg=bg, fg=fg, relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=10, pady=8, cursor="hand2",
            activebackground=BTN_ACTIVE, activeforeground=fg
        )

    # ─── Sample-PDF Hilfsmethoden ────────────────────────────────
    def _get_samples_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "samples"

    def _load_sample_pdfs(self) -> list:
        """Gibt sortierte Liste der PDF-Dateinamen aus data/samples/ zurück."""
        d = self._get_samples_dir()
        if not d.exists():
            return []
        return sorted(f.name for f in d.iterdir() if f.suffix.lower() == ".pdf")

    def _on_sample_selected(self, event=None):
        """Füllt Pfadfeld und ISIN automatisch aus wenn Sample gewählt wird."""
        name = self.var_sample.get()
        if not name or name == "(kein Sample gefunden)":
            return
        full_path = str(self._get_samples_dir() / name)
        self.var_single_pdf.set(full_path)
        if not self.var_single_isin.get():
            isin_match = re.search(r'[A-Z]{2}[A-Z0-9]{10}', Path(name).stem)
            if isin_match:
                self.var_single_isin.set(isin_match.group())
        if not self.var_single_name.get():
            stem = re.sub(r'^[\d_]+', '', Path(name).stem)
            stem = re.sub(r'[_-]+', ' ', stem).strip()
            if stem:
                self.var_single_name.set(stem)

    def _refresh_samples(self):
        """Aktualisiert Dropdown-Werte."""
        names = self._load_sample_pdfs()
        values = names if names else ["(kein Sample gefunden)"]
        self.sample_combo["values"] = values
        self.var_sample.set("")

    def _build_single_pdf_panel(self, parent):
        inner = self._panel(parent, "📥 Einzelne PDF analysieren (Prototyp)")

        tk.Label(
            inner, text="PDF-Datei auswählen und direkt analysieren.\nKein Excel nötig.",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8), justify="left"
        ).pack(anchor="w", pady=(0, 8))

        # ── Schnellauswahl aus data/samples/ ──────────────────────
        sample_row = tk.Frame(inner, bg=BG_PANEL)
        sample_row.pack(fill="x", pady=(0, 6))
        sample_row.columnconfigure(1, weight=1)

        tk.Label(
            sample_row, text="Beispiel-PDF:", bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 9), anchor="w"
        ).grid(row=0, column=0, sticky="w")

        self.var_sample = tk.StringVar()
        sample_names = self._load_sample_pdfs()
        combo_values = sample_names if sample_names else ["(kein Sample gefunden)"]

        self.sample_combo = ttk.Combobox(
            sample_row, textvariable=self.var_sample,
            values=combo_values, state="readonly",
            font=("Segoe UI", 9)
        )
        self.sample_combo.grid(row=0, column=1, sticky="ew", padx=(8, 4))
        self.sample_combo.bind("<<ComboboxSelected>>", self._on_sample_selected)

        tk.Button(
            sample_row, text="↺", command=self._refresh_samples,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 9), padx=5, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).grid(row=0, column=2)

        ttk.Separator(inner, orient="horizontal").pack(fill="x", pady=(0, 6))

        fields = tk.Frame(inner, bg=BG_PANEL)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)

        self.var_single_pdf = tk.StringVar()
        self.var_single_isin = tk.StringVar()
        self.var_single_name = tk.StringVar()

        tk.Label(fields, text="PDF:", bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9)).grid(
            row=0, column=0, sticky="w", pady=2)
        tk.Entry(fields, textvariable=self.var_single_pdf, bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, font=("Segoe UI", 9), relief="flat", bd=4
                 ).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=2)
        tk.Button(fields, text="...", command=self._browse_single_pdf,
                  bg=BTN_BG, fg=FG_TEXT, relief="flat", font=("Segoe UI", 8), padx=6,
                  cursor="hand2", activebackground=BTN_ACTIVE
                  ).grid(row=0, column=2, padx=(4, 0), pady=2)

        tk.Label(fields, text="ISIN:", bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9)).grid(
            row=1, column=0, sticky="w", pady=2)
        tk.Entry(fields, textvariable=self.var_single_isin, bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, font=("Segoe UI", 9), relief="flat", bd=4
                 ).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)

        tk.Label(fields, text="Name:", bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9)).grid(
            row=2, column=0, sticky="w", pady=2)
        tk.Entry(fields, textvariable=self.var_single_name, bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, font=("Segoe UI", 9), relief="flat", bd=4
                 ).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2)

        tk.Button(
            inner, text="🔍  Jetzt analysieren", command=self._analyze_single_pdf,
            bg="#1e2a3e", fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 10, "bold"), padx=10, pady=6,
            cursor="hand2", activebackground=BTN_ACTIVE
        ).pack(fill="x", pady=(8, 0))

        # Ergebnis-Anzeige
        self.result_frame = tk.Frame(inner, bg=BG_PANEL)
        self.result_frame.pack(fill="x", pady=(8, 0))

    def _build_log_panel(self, parent):
        tk.Label(
            parent, text="📋 Protokoll", bg=BG_MAIN, fg=ACCENT_BLUE,
            font=("Segoe UI", 10, "bold"), anchor="w"
        ).pack(anchor="w", pady=(0, 5))

        # Fortschrittsbalken
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            parent, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.pack(fill="x", pady=(0, 4))

        # Live-Status-Panel
        live = tk.Frame(parent, bg=BG_PANEL, pady=4)
        live.pack(fill="x", pady=(0, 6))

        top_row = tk.Frame(live, bg=BG_PANEL)
        top_row.pack(fill="x", padx=10, pady=(2, 0))
        tk.Label(top_row, text="Aktuell:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(top_row, textvariable=self._current_isin_var, bg=BG_PANEL,
                 fg=ACCENT_BLUE, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(6, 0))

        self._step_label = tk.Label(live, textvariable=self._current_step_var,
                                    bg=BG_PANEL, fg=FG_MUTED,
                                    font=("Segoe UI", 8), anchor="w")
        self._step_label.pack(fill="x", padx=10, pady=(1, 3))

        self.progress_label = tk.Label(
            parent, text="", bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8)
        )
        self.progress_label.pack(anchor="w", pady=(0, 4))

        # Log-Textfeld
        self.log_text = scrolledtext.ScrolledText(
            parent, bg=BG_PANEL, fg=FG_TEXT,
            font=("Consolas", 9), relief="flat",
            state="disabled", wrap="word"
        )
        self.log_text.pack(fill="both", expand=True)

        # Log-Tags für Farben
        self.log_text.tag_config("ok",     foreground=ACCENT_GREEN)
        self.log_text.tag_config("error",  foreground=ACCENT_RED)
        self.log_text.tag_config("warn",   foreground=ACCENT_YELLOW)
        self.log_text.tag_config("info",   foreground=ACCENT_BLUE)
        self.log_text.tag_config("muted",  foreground=FG_MUTED)
        self.log_text.tag_config("rule",   foreground=ACCENT_LAVENDER)
        self.log_text.tag_config("llm",    foreground="#cba6f7")   # lila
        self.log_text.tag_config("detail", foreground="#6c7086")   # grau (Einrückungen)

        # Button-Leiste unter dem Log
        btn_row = tk.Frame(parent, bg=BG_MAIN)
        btn_row.pack(fill="x", pady=(5, 0))

        tk.Button(
            btn_row, text="🗑 Log leeren", command=self._clear_log,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 8), padx=6, pady=3, cursor="hand2"
        ).pack(side="left")

        tk.Button(
            btn_row, text="📊 Excel öffnen", command=self._open_excel,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 8), padx=6, pady=3, cursor="hand2"
        ).pack(side="right")

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="Bereit")
        status_bar = tk.Label(
            self, textvariable=self.status_var,
            bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w", padx=10, pady=3
        )
        status_bar.pack(fill="x", side="bottom")

    # ─── Einstellungen speichern/laden ────────────────────────────

    def _load_settings(self):
        """Lädt gespeicherte Einstellungen aus .env."""
        self.var_api_key.set(os.getenv("ANTHROPIC_API_KEY", ""))
        self.var_excel.set(os.getenv("EXCEL_PATH", "data/input/fonds_universe.xlsx"))
        self.var_pdf_folder.set(os.getenv("PDF_FOLDER", "data/prospectus"))
        self.var_batch_size.set(os.getenv("BATCH_SIZE", "200"))

    def _save_settings(self):
        """Speichert die aktuellen Einstellungen."""
        env_path = Path(".env")
        if not env_path.exists():
            env_path.write_text("")

        api_key = self.var_api_key.get().strip()
        if api_key:
            set_key(".env", "ANTHROPIC_API_KEY", api_key)
        set_key(".env", "EXCEL_PATH", self.var_excel.get().strip())
        set_key(".env", "PDF_FOLDER", self.var_pdf_folder.get().strip())
        set_key(".env", "BATCH_SIZE", self.var_batch_size.get().strip())

    # ─── Browse-Dialoge ───────────────────────────────────────────

    def _browse_excel(self):
        path = filedialog.askopenfilename(
            title="Excel-Datei wählen",
            filetypes=[("Excel", "*.xlsx *.xls"), ("Alle Dateien", "*.*")]
        )
        if path:
            self.var_excel.set(path)

    def _browse_folder(self):
        path = filedialog.askdirectory(title="PDF-Ordner wählen")
        if path:
            self.var_pdf_folder.set(path)

    def _browse_single_pdf(self):
        path = filedialog.askopenfilename(
            title="PDF-Datei wählen",
            filetypes=[("PDF", "*.pdf"), ("Alle Dateien", "*.*")]
        )
        if path:
            self.var_single_pdf.set(path)
            # ISIN aus Dateiname ableiten (falls vorhanden)
            stem = Path(path).stem
            isin_match = re.search(r'[A-Z]{2}[A-Z0-9]{10}', stem)
            if isin_match and not self.var_single_isin.get():
                self.var_single_isin.set(isin_match.group())

    # ─── Log-Funktionen ───────────────────────────────────────────

    def _log_tag(self, msg: str) -> str:
        """Wählt den passenden Farb-Tag anhand der Nachrichteninhalts."""
        if "❌" in msg:
            return "error"
        if "✅ Regelbasiert" in msg or "✅ PDF" in msg:
            return "ok"
        if "✅" in msg:
            return "ok"
        if "📐" in msg or "Regelextraktor" in msg or "Fondstyp:" in msg \
                or "Anlegertyp:" in msg or "Kundentyp:" in msg \
                or "Segmentierung:" in msg or "Begründung:" in msg \
                or "Regelbasiert →" in msg:
            return "rule"
        if "🤖" in msg or "LLM" in msg:
            return "llm"
        if "⤵" in msg or "✂" in msg:
            return "warn"
        if msg.startswith("     ") or msg.strip().startswith("Begründung"):
            return "detail"
        return ""

    def _log(self, message: str, tag: str = ""):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ─── Batch-Analyse ────────────────────────────────────────────

    def _start_batch(self):
        if self._running:
            return

        # Einstellungen prüfen
        excel_path = self.var_excel.get().strip()
        api_key = self.var_api_key.get().strip()

        if not excel_path:
            messagebox.showerror("Fehler", "Bitte Excel-Datei wählen.")
            return
        if not api_key:
            messagebox.showerror("Fehler", "Bitte API-Key eingeben.")
            return

        self._save_settings()
        self._running = True
        self._batch_results = []
        self._batch_start_time = time.time()
        self._current_isin_var.set("—")
        self._current_step_var.set("Starte...")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.progress_var.set(0)

        from main import BatchProcessor, Config

        try:
            batch_size = int(self.var_batch_size.get())
        except ValueError:
            batch_size = 200

        config = Config(
            excel_path=excel_path,
            pdf_folder=self.var_pdf_folder.get().strip() or "data/prospectus",
            batch_size=batch_size,
            api_key=api_key,
        )

        self._progress_queue = queue.Queue()
        self._processor = BatchProcessor(config, self._progress_queue)

        self._batch_thread = threading.Thread(target=self._processor.run, daemon=True)
        self._batch_thread.start()
        self._log("▶ Batch-Analyse gestartet...", "info")

    def _stop_batch(self):
        if self._processor:
            self._processor.stop()
        self.status_var.set("⏸ Wird angehalten...")

    # ─── Einzelne PDF analysieren ─────────────────────────────────

    def _analyze_single_pdf(self):
        pdf_path = self.var_single_pdf.get().strip()
        if not pdf_path:
            messagebox.showerror("Fehler", "Bitte eine PDF-Datei wählen.")
            return

        api_key = self.var_api_key.get().strip() or os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror("Fehler", "Bitte API-Key eingeben.")
            return

        isin = self.var_single_isin.get().strip()
        fund_name = self.var_single_name.get().strip()

        self._save_settings()
        self._clear_result_display()
        self._log(f"🔍 Analysiere: {Path(pdf_path).name}", "info")
        self.status_var.set(f"Analysiere {Path(pdf_path).name}...")

        # ── Pipeline-Fenster öffnen ───────────────────────────────
        from analysis_workflow import AnalysisWorkflowWindow
        workflow = AnalysisWorkflowWindow(
            self,
            pdf_name=Path(pdf_path).name,
            isin=isin,
            fund_name=fund_name,
        )

        # In Hintergrund-Thread
        def run():
            try:
                from main import process_single_pdf

                # Kombinierter Callback: bestehender Log + Pipeline-Fenster
                def cb(msg: str):
                    tag = self._log_tag(msg)
                    self.after(0, lambda m=msg, t=tag: self._log(m, t))
                    if workflow.winfo_exists():
                        self.after(0, lambda m=msg: workflow.handle_message(m))

                result = process_single_pdf(
                    pdf_path, isin=isin, fund_name=fund_name,
                    api_key=api_key, log_callback=cb
                )
                self.after(0, lambda: self._show_single_result(result))
                if workflow.winfo_exists():
                    self.after(0, lambda: workflow.show_result(result))
                # Ergebnis dauerhaft speichern
                self.after(0, lambda: self._store_result(
                    isin=isin,
                    fondsname=fund_name,
                    result=result,
                    pdf_datei=Path(pdf_path).name,
                ))
            except Exception as e:
                err_msg = str(e)
                self.after(0, lambda m=err_msg: self._log(f"❌ Fehler: {m}", "error"))
                self.after(0, lambda m=err_msg: self.status_var.set(f"Fehler: {m[:60]}"))
                if workflow.winfo_exists():
                    self.after(0, lambda m=err_msg: workflow.handle_message(f"❌ Fehler: {m}"))

        threading.Thread(target=run, daemon=True).start()

    def _clear_result_display(self):
        for w in self.result_frame.winfo_children():
            w.destroy()

    def _show_single_result(self, result: dict):
        """Zeigt das Klassifizierungsergebnis im GUI an."""
        seg = result.get("segmentierung", "unklar")
        konfidenz = result.get("konfidenz", "")

        # Farbe nach Segmentierung
        if seg == "institutional":
            seg_color = ACCENT_BLUE
        elif seg == "retail":
            seg_color = ACCENT_GREEN
        else:
            seg_color = ACCENT_YELLOW

        self._clear_result_display()

        # Titel
        tk.Label(
            self.result_frame, text="📊 Ergebnis",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8, "bold"), anchor="w"
        ).pack(anchor="w")

        # Hauptergebnis
        result_display = tk.Frame(self.result_frame, bg=BG_INPUT)
        result_display.pack(fill="x", pady=(4, 0))

        fields = [
            ("Segmentierung", seg.upper(), seg_color),
            ("Fondstyp", result.get("fondstyp", "-"), FG_TEXT),
            ("Anlegertyp", result.get("anlegertyp", "-"), FG_TEXT),
            ("Kundentyp", result.get("kundentyp", "-"), FG_TEXT),
            ("Konfidenz", konfidenz, ACCENT_YELLOW if konfidenz == "niedrig" else FG_TEXT),
        ]

        for label, value, color in fields:
            row = tk.Frame(result_display, bg=BG_INPUT)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text=f"{label}:", bg=BG_INPUT, fg=FG_MUTED,
                     font=("Segoe UI", 8), width=14, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=BG_INPUT, fg=color,
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(side="left")

        # Begründung
        beg = result.get("begruendung", "")
        if beg:
            tk.Label(
                self.result_frame,
                text=f"💬 {beg}",
                bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8),
                wraplength=340, justify="left", anchor="w"
            ).pack(anchor="w", pady=(4, 0))

        self._log(
            f"✅ Ergebnis: {seg.upper()} | {result.get('fondstyp', '')} | Konfidenz: {konfidenz}",
            "ok" if seg != "unklar" else "warn"
        )
        self.status_var.set(f"Fertig: {seg.upper()} ({konfidenz})")

    # ─── Event-Queue pollen ───────────────────────────────────────

    def _poll_queue(self):
        """Verarbeitet Events aus dem Hintergrund-Thread."""
        try:
            while True:
                event = self._progress_queue.get_nowait()

                if event.type == "log":
                    tag = self._log_tag(event.message)
                    self._log(event.message, tag)
                    # Schritt-Text aus Log-Nachricht ableiten
                    msg = event.message.strip()
                    if "📥" in msg:
                        self._current_step_var.set("📥 PDF wird heruntergeladen...")
                    elif "✅ PDF:" in msg:
                        self._current_step_var.set("📄 " + msg.lstrip("✅ "))
                    elif "🔍" in msg and "Web" in msg:
                        self._current_step_var.set("🔍 Web-Suche läuft...")
                    elif "📐" in msg:
                        self._current_step_var.set("📐 Regelextraktor läuft...")
                    elif "🤖 LLM-Aufruf" in msg:
                        self._current_step_var.set(msg.strip())
                    elif "🤖 LLM-Ergebnis" in msg:
                        self._current_step_var.set(msg.strip())
                    elif "❌ Konnte" in msg:
                        self._current_step_var.set("❌ Klassifizierung fehlgeschlagen")

                elif event.type == "progress":
                    if event.total > 0:
                        pct = (event.current / event.total) * 100
                        self.progress_var.set(pct)
                    self.progress_label.config(
                        text=f"{event.current} / {event.total} ISINs"
                    )
                    self._current_isin_var.set(event.isin)
                    self._current_step_var.set("🔄 Verarbeitung startet...")
                    self.status_var.set(event.message[:80])

                elif event.type == "result":
                    seg = event.result.get("segmentierung", "")
                    tag = "ok" if seg in ("institutional", "retail") else "warn"
                    self._log(event.message, tag)
                    self._batch_results.append(event.result)
                    # Ergebnis dauerhaft speichern
                    self._store_result(
                        isin=event.isin,
                        fondsname=event.result.get("_fund_name", ""),
                        result=event.result,
                        pdf_datei=event.result.get("_pdf_datei", ""),
                    )
                    # Schritt: Klassifizierung abgeschlossen
                    label = {"institutional": "INSTITUTIONAL", "retail": "RETAIL"}.get(seg, "UNKLAR")
                    conf = event.result.get("konfidenz", "")
                    src = event.result.get("_source", "llm")
                    src_label = "Regelbasiert" if src == "rules" else "LLM"
                    self._current_step_var.set(f"✅ {label} | {conf} | {src_label}")

                elif event.type == "done":
                    self._log(f"\n🎉 {event.message}", "ok")
                    self.progress_var.set(100)
                    self._current_isin_var.set("—")
                    self._current_step_var.set("Abgeschlossen")
                    self.status_var.set(event.message)
                    self._running = False
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.after(300, self._show_summary)

                elif event.type == "error":
                    self._log(f"❌ {event.message}", "error")
                    self.status_var.set(f"Fehler: {event.message[:60]}")
                    self._current_step_var.set(f"❌ {event.message[:60]}")
                    self._running = False
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")

        except queue.Empty:
            pass

        self.after(200, self._poll_queue)

    # ─── Batch-Zusammenfassung ────────────────────────────────────

    def _show_summary(self):
        results = self._batch_results
        if not results:
            return

        elapsed = int(time.time() - (self._batch_start_time or time.time()))
        mins, secs = divmod(elapsed, 60)

        total = len(results)
        retail = sum(1 for r in results if r.get("segmentierung") == "retail")
        institutional = sum(1 for r in results if r.get("segmentierung") == "institutional")
        unklar = sum(1 for r in results if r.get("segmentierung") == "unklar")
        fehler = sum(1 for r in results if r.get("segmentierung") in ("fehler", "error", ""))
        rules_count = sum(1 for r in results if r.get("_source") == "rules")
        llm_count = total - rules_count

        win = tk.Toplevel(self)
        win.title("Batch-Zusammenfassung")
        win.configure(bg=BG_MAIN)
        win.geometry("460x420")
        win.resizable(False, False)
        win.grab_set()

        # Titel
        tk.Label(win, text="Batch-Analyse abgeschlossen",
                 bg=BG_MAIN, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 13, "bold")).pack(pady=(18, 4))

        tk.Label(win, text=f"Laufzeit: {mins}m {secs:02d}s  |  {total} ISINs verarbeitet",
                 bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9)).pack(pady=(0, 12))

        def section(parent, title):
            tk.Label(parent, text=title, bg=BG_MAIN, fg=ACCENT_BLUE,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", padx=24, pady=(8, 2))
            ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=24)

        def bar_row(parent, label, count, color):
            if total == 0:
                return
            pct = count / total
            bar_len = 24
            filled = round(pct * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            row = tk.Frame(parent, bg=BG_MAIN)
            row.pack(fill="x", padx=24, pady=2)
            tk.Label(row, text=f"{label:<16}", bg=BG_MAIN, fg=FG_MUTED,
                     font=("Consolas", 9), width=16, anchor="w").pack(side="left")
            tk.Label(row, text=bar, bg=BG_MAIN, fg=color,
                     font=("Consolas", 9)).pack(side="left")
            tk.Label(row, text=f"  {count:>3}  ({pct:.0%})",
                     bg=BG_MAIN, fg=FG_TEXT, font=("Consolas", 9)).pack(side="left")

        section(win, "Segmentierung")
        bar_row(win, "Retail", retail, ACCENT_GREEN)
        bar_row(win, "Institutional", institutional, ACCENT_BLUE)
        bar_row(win, "Unklar", unklar, ACCENT_YELLOW)
        if fehler:
            bar_row(win, "Fehler", fehler, ACCENT_RED)

        section(win, "Verarbeitungsmethode")
        bar_row(win, "Regelbasiert", rules_count, ACCENT_GREEN)
        bar_row(win, "LLM-Fallback", llm_count, ACCENT_YELLOW)

        if rules_count > 0:
            token_saved = rules_count * 8000
            tk.Label(win,
                     text=f"~{token_saved:,} Zeichen eingespart (kein LLM für {rules_count} ISINs)",
                     bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8)).pack(pady=(6, 0))

        tk.Button(
            win, text="  OK  ", command=win.destroy,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 10), padx=20, pady=6, cursor="hand2",
            activebackground=BTN_ACTIVE
        ).pack(pady=18)

    # ─── Ergebnis-Datenbank ───────────────────────────────────────

    def _store_result(self, isin: str, fondsname: str, result: dict, pdf_datei: str = ""):
        """Speichert ein Ergebnis in der SQLite-DB und aktualisiert offene Fenster."""
        try:
            import results_store
            results_store.upsert_result(isin, fondsname, result, pdf_datei)
        except Exception:
            pass
        # Offenes Ergebnis-Fenster sofort aktualisieren
        try:
            if self._results_win and self._results_win.winfo_exists():
                self._results_win.refresh()
        except Exception:
            pass
        # Datenverwaltungs-Ergebnis-Tab live aktualisieren
        try:
            if self._data_mgmt_win and self._data_mgmt_win.winfo_exists():
                self._data_mgmt_win.refresh_results()
        except Exception:
            pass

    def _open_results(self):
        from results_window import ResultsWindow
        if self._results_win and self._results_win.winfo_exists():
            self._results_win.lift()
            self._results_win.focus_force()
        else:
            self._results_win = ResultsWindow(self)

    def _open_data_management(self):
        from data_management_window import DataManagementWindow
        if self._data_mgmt_win and self._data_mgmt_win.winfo_exists():
            self._data_mgmt_win.lift()
            self._data_mgmt_win.focus_force()
        else:
            self._data_mgmt_win = DataManagementWindow(self)

    def _open_download_window(self):
        from download_window import DownloadWindow
        if self._download_win and self._download_win.winfo_exists():
            self._download_win.lift()
            self._download_win.focus_force()
        else:
            self._download_win = DownloadWindow(self)

    def _open_analysis_window(self):
        from prospekt_analysis_window import ProspektAnalysisWindow
        if self._analysis_win and self._analysis_win.winfo_exists():
            self._analysis_win.lift()
            self._analysis_win.focus_force()
        else:
            self._analysis_win = ProspektAnalysisWindow(self)

    # ─── Sonstiges ────────────────────────────────────────────────

    def _open_admin(self):
        from admin_panel import AdminPanel
        AdminPanel(self)

    def _open_excel(self):
        excel_path = self.var_excel.get().strip()
        if excel_path and os.path.exists(excel_path):
            os.startfile(excel_path)
        else:
            messagebox.showinfo("Info", "Excel-Datei nicht gefunden.")


def main():
    # Arbeitsverzeichnis auf Projektroot setzen
    project_root = Path(__file__).parent.parent
    os.chdir(project_root)

    # Verzeichnisse anlegen
    for d in ["data/input", "data/output", "data/prospectus"]:
        Path(d).mkdir(parents=True, exist_ok=True)

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
