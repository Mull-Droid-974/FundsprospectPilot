"""
Prospekt-Downloader Fenster.

Lädt Verkaufsprospekte für ISINs aus der Ergebnisdatenbank herunter.
Öffnet sich als Toplevel aus app.py; kann auch standalone genutzt werden.
"""

import os
import queue
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import results_store
from prospekt_worker import ProspektEvent, ProspektWorker

# ─── Farben (identisch zu app.py) ────────────────────────────────────────────
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

_COLS = [
    ("isin",          "ISIN",           130),
    ("subfonds_name", "Unterfonds",     220),
    ("anteilsklasse", "Anteilsklasse",  150),
    ("prospekt_pfad", "Prospekt-Datei", 200),
    ("prospekt_url",  "Prospekt-URL",   180),
]


class DownloadWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget, pdf_folder: Path | None = None):
        super().__init__(parent)
        self.title("Prospekt-Downloader")
        self.configure(bg=BG_MAIN)
        self.geometry("900x650")
        self.minsize(700, 500)

        self._pdf_folder = pdf_folder or _PDF_FOLDER
        self._worker: ProspektWorker | None = None
        self._event_queue: queue.Queue = queue.Queue()
        self._all_rows: list[dict] = []

        self._build_ui()
        self._refresh_table()
        self._poll_queue()

    # ─── UI aufbauen ──────────────────────────────────────────────────────────

    def _build_ui(self):
        # Toolbar
        toolbar = tk.Frame(self, bg=BG_PANEL)
        toolbar.pack(fill="x")
        inner = tk.Frame(toolbar, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        tk.Label(
            inner, text="📄 Prospekt-Downloader",
            bg=BG_PANEL, fg=ACCENT_LAVENDER,
            font=("Segoe UI", 11, "bold")
        ).pack(side="left")

        self.btn_stop = tk.Button(
            inner, text="⏹  Stopp",
            command=self._stop_worker,
            bg="#3a2000", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE, state="disabled"
        )
        self.btn_stop.pack(side="right", padx=(4, 0))

        self.btn_batch = tk.Button(
            inner, text="📥  Alle fehlenden downloaden",
            command=self._start_batch,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        )
        self.btn_batch.pack(side="right", padx=(4, 0))

        # Einzel-Download-Zeile
        single_frame = tk.Frame(self, bg=BG_PANEL)
        single_frame.pack(fill="x", padx=0)
        single_inner = tk.Frame(single_frame, bg=BG_PANEL)
        single_inner.pack(fill="x", padx=12, pady=(0, 8))

        tk.Label(
            single_inner, text="Einzel-Download — ISIN:",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")

        self._isin_var = tk.StringVar()
        tk.Entry(
            single_inner, textvariable=self._isin_var,
            bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
            font=("Segoe UI", 9), relief="flat", bd=4, width=18
        ).pack(side="left", padx=(6, 4))

        self.btn_single = tk.Button(
            single_inner, text="▶  Starten",
            command=self._start_single,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=8, pady=2, cursor="hand2",
            activebackground=BTN_ACTIVE
        )
        self.btn_single.pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # Fortschrittszeile
        prog_frame = tk.Frame(self, bg=BG_MAIN)
        prog_frame.pack(fill="x", padx=12, pady=(6, 4))

        self._prog_var = tk.DoubleVar(value=0)
        self._prog_bar = ttk.Progressbar(
            prog_frame, variable=self._prog_var, maximum=100, length=300
        )
        self._prog_bar.pack(side="left", padx=(0, 10))

        self._status_var = tk.StringVar(value="Bereit")
        tk.Label(
            prog_frame, textvariable=self._status_var,
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")

        self._refresh_btn = tk.Button(
            prog_frame, text="↺ Aktualisieren",
            command=self._refresh_table,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2",
            activebackground=BTN_ACTIVE
        )
        self._refresh_btn.pack(side="right")

        # Treeview
        tree_frame = tk.Frame(self, bg=BG_MAIN)
        tree_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        cols = [c[0] for c in _COLS]
        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="browse")

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
                        background=BG_PANEL, foreground=FG_TEXT,
                        fieldbackground=BG_PANEL, rowheight=24,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background=BG_INPUT, foreground=ACCENT_BLUE,
                        font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3a3a5e")])

        for key, header, width in _COLS:
            self._tree.heading(key, text=header,
                               command=lambda k=key: self._sort_by(k))
            self._tree.column(key, width=width, minwidth=60, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._tree.pack(fill="both", expand=True)

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-1>", self._on_click)

        self._tree.tag_configure("ok",      background="#1a2e1a", foreground=ACCENT_GREEN)
        self._tree.tag_configure("missing", background=BG_PANEL,  foreground=FG_MUTED)

        # Log-Bereich
        tk.Label(
            self, text="Log", bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w"
        ).pack(fill="x", padx=12)

        self._log = scrolledtext.ScrolledText(
            self, height=7, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat"
        )
        self._log.pack(fill="x", padx=12, pady=(0, 8))

    # ─── Tabelle ──────────────────────────────────────────────────────────────

    def _refresh_table(self):
        self._all_rows = results_store.get_all_results()
        self._populate_tree(self._all_rows)

    def _populate_tree(self, rows: list[dict]):
        self._tree.delete(*self._tree.get_children())
        for row in rows:
            pfad = row.get("prospekt_pfad", "") or ""
            url  = row.get("prospekt_url",  "") or ""
            exists = bool(pfad) and Path(pfad).exists()
            display_pfad = ("✓ " + Path(pfad).name) if exists else "—"
            tag = "ok" if exists else "missing"
            self._tree.insert("", "end", iid=row["isin"], values=(
                row.get("isin", ""),
                row.get("subfonds_name", "") or "—",
                row.get("anteilsklasse", "") or "—",
                display_pfad,
                url or "—",
            ), tags=(tag,))

    def _sort_by(self, col: str):
        items = [(self._tree.set(iid, col), iid) for iid in self._tree.get_children()]
        items.sort(key=lambda x: x[0].lower())
        for idx, (_, iid) in enumerate(items):
            self._tree.move(iid, "", idx)

    def _on_double_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item:
            return
        row = results_store.get_result(item)
        if not row:
            return
        pfad = row.get("prospekt_pfad", "") or ""
        if pfad and Path(pfad).exists():
            os.startfile(pfad)
        else:
            messagebox.showinfo("Kein Prospekt", f"Kein Prospekt für {item} vorhanden.", parent=self)

    def _on_click(self, event):
        col = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if not item or col != "#4":
            return
        row = results_store.get_result(item)
        if not row:
            return
        url = row.get("prospekt_url", "") or ""
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self._log_line(f"URL kopiert: {url}")

    # ─── Worker starten/stoppen ───────────────────────────────────────────────

    def _start_batch(self):
        queue_rows = results_store.get_prospekt_queue()
        if not queue_rows:
            messagebox.showinfo("Keine ISINs", "Alle ISINs haben bereits ein Prospekt.", parent=self)
            return
        self._start_worker(queue_rows)

    def _start_single(self):
        isin = self._isin_var.get().strip().upper()
        if not isin:
            messagebox.showwarning("ISIN fehlt", "Bitte eine ISIN eingeben.", parent=self)
            return
        row = results_store.get_result(isin)
        if not row:
            messagebox.showwarning("Nicht gefunden", f"ISIN {isin} ist nicht in der Datenbank.", parent=self)
            return
        self._start_worker([row])

    def _start_worker(self, rows: list[dict]):
        if self._worker and self._worker.is_alive():
            return
        self._event_queue = queue.Queue()
        self._worker = ProspektWorker(rows, self._pdf_folder, self._event_queue)
        self._worker.start()
        self._set_running(True)
        self._status_var.set(f"0 / {len(rows)}")
        self._prog_var.set(0)
        self._log_line(f"Starte Download für {len(rows)} ISIN(s) …")

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()

    def _set_running(self, running: bool):
        state_on  = "disabled" if running else "normal"
        state_off = "normal"   if running else "disabled"
        self.btn_batch.config(state=state_on)
        self.btn_single.config(state=state_on)
        self.btn_stop.config(state=state_off)

    # ─── Queue-Polling ────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                evt: ProspektEvent = self._event_queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _handle_event(self, evt: ProspektEvent):
        if evt.type == "log":
            self._log_line(f"[{evt.isin}] {evt.message}" if evt.isin else evt.message)

        elif evt.type in ("progress", "error"):
            phase_label = f"P{evt.phase}"
            prefix = "✓" if evt.type == "progress" else "✗"
            self._log_line(f"{prefix} [{phase_label}] [{evt.isin}] {evt.message}")
            if evt.total > 0:
                pct = (evt.done + evt.skipped + evt.failed) / evt.total * 100
                self._prog_var.set(pct)
                phase_str = "Metadaten" if evt.phase == 1 else "Downloads"
                self._status_var.set(
                    f"Phase {evt.phase} {phase_str}: "
                    f"{evt.done + evt.skipped + evt.failed}/{evt.total}  "
                    f"✓{evt.done}  ✗{evt.failed}  ⟳{evt.skipped}"
                )
            # Zeile im Treeview aktualisieren
            row = results_store.get_result(evt.isin)
            if row:
                pfad = row.get("prospekt_pfad", "") or ""
                url  = row.get("prospekt_url",  "") or ""
                exists = bool(pfad) and Path(pfad).exists()
                display_pfad = ("✓ " + Path(pfad).name) if exists else "—"
                try:
                    self._tree.item(evt.isin, values=(
                        row.get("isin", ""),
                        row.get("subfonds_name", "") or "—",
                        row.get("anteilsklasse", "") or "—",
                        display_pfad,
                        url or "—",
                    ), tags=("ok" if exists else "missing",))
                except tk.TclError:
                    pass

        elif evt.type == "done":
            self._log_line(f"✅ {evt.message}")
            self._status_var.set(evt.message)
            self._prog_var.set(100)
            self._set_running(False)
            self._refresh_table()

    def _log_line(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")
