"""
Prospekt-Downloader Fenster.

Lädt Verkaufsprospekte für ISINs aus der Ergebnisdatenbank herunter.
Zeigt Fonds gruppiert nach Umbrella und Subfonds (zwei Ebenen).
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

# ─── Farben ───────────────────────────────────────────────────────────────────
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

_UMBRELLA_COLS = [
    ("name",   "Umbrella / Subfonds / ISIN", 380),
    ("klasse", "Anteilsklasse",              160),
    ("status", "Status",                     140),
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
        self._iid_to_rows: dict[str, list[dict]] = {}

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

        self._skip_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            inner, text="Nicht gef. überspringen",
            variable=self._skip_var,
            bg=BG_PANEL, fg=FG_MUTED, selectcolor=BG_INPUT,
            activebackground=BG_PANEL, activeforeground=FG_TEXT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="right", padx=(12, 4))

        self.btn_batch = tk.Button(
            inner, text="📥  Alle fehlenden downloaden",
            command=self._start_batch,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        )
        self.btn_batch.pack(side="right", padx=(4, 0))

        self.btn_phase2 = tk.Button(
            inner, text="⬇  Nur PDFs (Phase 2)",
            command=self._start_phase2_only,
            bg="#1a1e2e", fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        )
        self.btn_phase2.pack(side="right", padx=(4, 0))

        self.btn_sel = tk.Button(
            inner, text="▶  Auswahl downloaden",
            command=self._start_selection,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE
        )
        self.btn_sel.pack(side="right", padx=(4, 0))

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

        cols = [c[0] for c in _UMBRELLA_COLS]
        self._tree = ttk.Treeview(
            tree_frame, columns=cols, show="tree headings",
            selectmode="extended"
        )

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

        # hide the tree column (chevron column), use name column instead
        self._tree.column("#0", width=0, minwidth=0, stretch=False)

        for key, header, width in _UMBRELLA_COLS:
            self._tree.heading(key, text=header)
            self._tree.column(key, width=width, minwidth=40, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._tree.pack(fill="both", expand=True)

        self._tree.tag_configure("umbrella",  background=BG_INPUT,   foreground=ACCENT_LAVENDER)
        self._tree.tag_configure("ok",        background="#1a2e1a",   foreground=ACCENT_GREEN)
        self._tree.tag_configure("missing",   background=BG_PANEL,    foreground=FG_MUTED)
        self._tree.tag_configure("not_found", background="#2e1a1a",   foreground=ACCENT_RED)

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-1>", self._on_click)

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

    def _group_rows(self, rows: list[dict]) -> dict:
        """Gruppiert Zeilen nach umbrella_id → subfonds_name."""
        groups: dict[str, dict] = {}
        for row in rows:
            u_key = (row.get("umbrella_id") or "").strip() or "—"
            # Sentinel-subfonds_id (__nf_*) ignorieren
            sf_id_raw = (row.get("subfonds_id") or "").strip()
            if sf_id_raw.startswith("__"):
                sf_id_raw = ""
            # subfonds_name als Schlüssel — identisch für alle Anteilsklassen eines Subfonds
            sf_key = (row.get("subfonds_name") or "").strip() or sf_id_raw or "—"
            if u_key not in groups:
                groups[u_key] = {"label": u_key, "subfonds": {}}
            if sf_key not in groups[u_key]["subfonds"]:
                groups[u_key]["subfonds"][sf_key] = {"label": sf_key, "rows": []}
            groups[u_key]["subfonds"][sf_key]["rows"].append(row)
        return groups

    @staticmethod
    def _row_status(row: dict) -> tuple[str, str]:
        pfad = row.get("prospekt_pfad") or ""
        if pfad and Path(pfad).exists():
            return "ok", "✓ PDF"
        if row.get("prospekt_nicht_gefunden"):
            return "not_found", "✗ Nicht gef."
        return "missing", "— Fehlt"

    def _populate_tree(self, rows: list[dict]):
        self._tree.delete(*self._tree.get_children())
        self._iid_to_rows = {}
        groups = self._group_rows(rows)

        for u_key, u_data in sorted(groups.items()):
            u_iid = f"u:{u_key}"
            sf_count  = len(u_data["subfonds"])
            isin_count = sum(len(v["rows"]) for v in u_data["subfonds"].values())
            self._tree.insert("", "end", iid=u_iid, open=True, values=(
                u_data["label"],
                f"{sf_count} Subfonds / {isin_count} ISINs",
                "",
            ), tags=("umbrella",))

            for sf_id, sf_data in sorted(u_data["subfonds"].items(),
                                          key=lambda x: x[1]["label"]):
                s_iid    = f"s:{u_key}:{sf_id}"
                sf_rows  = sf_data["rows"]
                sf_label = sf_data["label"]

                # Subfonds-Status: grün wenn mind. 1 PDF, rot wenn alle nicht gefunden
                has_pdf = any(bool(r.get("prospekt_pfad")) and Path(r["prospekt_pfad"]).exists() for r in sf_rows)
                has_nf  = all(r.get("prospekt_nicht_gefunden") for r in sf_rows)
                if has_pdf:
                    sf_tag, sf_status = "ok", "✓ PDF vorhanden"
                elif has_nf:
                    sf_tag, sf_status = "not_found", "✗ Nicht gefunden"
                else:
                    sf_tag, sf_status = "missing", "— Fehlt"

                self._tree.insert(u_iid, "end", iid=s_iid, open=True, values=(
                    f"  {sf_label}",
                    f"{len(sf_rows)} ISINs",
                    sf_status,
                ), tags=(sf_tag,))

                # ISIN-Blattknoten
                for row in sorted(sf_rows, key=lambda r: r.get("isin", "")):
                    isin  = row.get("isin", "")
                    i_iid = f"i:{isin}"
                    self._iid_to_rows[i_iid] = row
                    tag, status = self._row_status(row)
                    klasse = row.get("anteilsklasse") or "—"
                    self._tree.insert(s_iid, "end", iid=i_iid, values=(
                        f"    {isin}",
                        klasse,
                        status,
                    ), tags=(tag,))

    def _get_leaf_iids(self, iid: str) -> list[str]:
        """Gibt alle ISIN-Blattknoten (i:...) rekursiv zurück."""
        if iid.startswith("i:"):
            return [iid]
        result = []
        for child in self._tree.get_children(iid):
            result.extend(self._get_leaf_iids(child))
        return result

    def _on_double_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid or not iid.startswith("i:"):
            return
        row = self._iid_to_rows.get(iid)
        if not row:
            return
        pfad = row.get("prospekt_pfad", "") or ""
        if pfad and Path(pfad).exists():
            os.startfile(pfad)
        else:
            messagebox.showinfo("Kein Prospekt", f"Kein Prospekt für {row.get('isin', '')} vorhanden.", parent=self)

    def _on_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        if iid.startswith("u:") or iid.startswith("s:"):
            leaves = self._get_leaf_iids(iid)
            if not leaves:
                return
            current = set(self._tree.selection())
            self._tree.selection_remove(iid)
            if all(c in current for c in leaves):
                self._tree.selection_remove(*leaves)
            else:
                self._tree.selection_add(*leaves)

    # ─── Worker starten/stoppen ───────────────────────────────────────────────

    def _start_batch(self):
        queue_rows = results_store.get_prospekt_queue(skip_nicht_gefunden=self._skip_var.get())
        if not queue_rows:
            messagebox.showinfo("Keine ISINs", "Alle ISINs haben bereits ein Prospekt.", parent=self)
            return
        self._start_worker(queue_rows)

    def _start_phase2_only(self):
        rows = [
            r for r in results_store.get_prospekt_queue(skip_nicht_gefunden=self._skip_var.get())
            if r.get("subfonds_id")
        ]
        if not rows:
            messagebox.showinfo(
                "Keine ISINs",
                "Keine ISINs mit geladenen Metadaten gefunden.\n"
                "Bitte zuerst den vollen Batch starten, damit Phase 1 (Metadaten) läuft.",
                parent=self,
            )
            return
        self._start_worker(rows)

    def _start_selection(self):
        seen: set[str] = set()
        rows: list[dict] = []
        for iid in self._tree.selection():
            if iid.startswith("i:"):
                row = self._iid_to_rows.get(iid)
                if row and row.get("isin") not in seen:
                    seen.add(row["isin"])
                    rows.append(row)
        if not rows:
            messagebox.showwarning(
                "Keine Auswahl",
                "Bitte mindestens eine ISIN auswählen.\n"
                "Klick auf Umbrella oder Subfonds wählt alle ISINs darunter.",
                parent=self,
            )
            return
        self._start_worker(rows)

    def _start_worker(self, rows: list[dict], single_mode: bool = False):
        if self._worker and self._worker.is_alive():
            return
        self._event_queue = queue.Queue()
        self._worker = ProspektWorker(
            rows, self._pdf_folder, self._event_queue, single_mode=single_mode
        )
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
        self.btn_phase2.config(state=state_on)
        self.btn_sel.config(state=state_on)
        self.btn_stop.config(state=state_off)
        if hasattr(self.master, "notify_process"):
            self.master.notify_process("Download", running)

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
            # ISIN-Blattzeile live aktualisieren
            if evt.isin:
                i_iid = f"i:{evt.isin}"
                row = results_store.get_result(evt.isin)
                if row and self._tree.exists(i_iid):
                    tag, status = self._row_status(row)
                    try:
                        self._tree.item(i_iid, values=(
                            f"    {evt.isin}",
                            row.get("anteilsklasse") or "—",
                            status,
                        ), tags=(tag,))
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
