"""
FundsprospectPilot – Tkinter Desktop-App
Hub-Fenster mit Sidebar-Navigation und Dashboard.
"""

import os
import queue
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

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
        self.geometry("960x660")
        self.minsize(800, 580)
        self.configure(bg=BG_MAIN)

        # Sub-window references
        self._results_win = None
        self._data_mgmt_win = None
        self._download_win = None
        self._analysis_win = None
        self._pdf_trim_win = None
        self._comparison_win = None

        # Live-Status
        self._current_isin_var = tk.StringVar(value="—")
        self._current_step_var = tk.StringVar(value="Bereit")

        # Laufende Sub-Prozesse (Name → True)
        self._running_processes: set[str] = set()

        # Event queue (for background workers reporting back)
        self._progress_queue = queue.Queue()

        self._build_ui()
        self._poll_queue()

    # ─── UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_sidebar()
        content = tk.Frame(self, bg=BG_MAIN)
        content.pack(side="left", fill="both", expand=True)
        self._build_dashboard(content)
        self._build_log_panel(content)
        self._build_statusbar()

    def _build_sidebar(self):
        sb = tk.Frame(self, bg=BG_PANEL, width=160)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        # Logo
        logo = tk.Frame(sb, bg=BG_PANEL)
        logo.pack(fill="x", pady=(18, 10))
        tk.Label(logo, text="🏦", bg=BG_PANEL, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 22)).pack()
        tk.Label(logo, text="FundsPilot", bg=BG_PANEL, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 8, "bold")).pack()

        ttk.Separator(sb, orient="horizontal").pack(fill="x", padx=12, pady=(4, 6))

        # ── Vorbereitung ─────────────────────────────────────────────
        self._section_label(sb, "VORBEREITUNG")
        self._nav_btn(sb, "🗃",  "1. Daten",      self._open_data_management,  ACCENT_BLUE)
        self._nav_btn(sb, "📄", "2. Prospekte",  self._open_download_window,   ACCENT_YELLOW)

        ttk.Separator(sb, orient="horizontal").pack(fill="x", padx=12, pady=(8, 6))

        # ── Verarbeitung ─────────────────────────────────────────────
        self._section_label(sb, "VERARBEITUNG")
        self._nav_btn(sb, "✂",  "3. PDF-Kürzen", self._open_pdf_trim_window,  ACCENT_BLUE)
        tk.Label(sb, text="   (optional)", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 7, "italic"), anchor="w").pack(fill="x", padx=16)
        self._nav_btn(sb, "🔬", "4. Analyse",    self._open_analysis_window,   ACCENT_LAVENDER)

        ttk.Separator(sb, orient="horizontal").pack(fill="x", padx=12, pady=(8, 6))

        # ── Auswertung ───────────────────────────────────────────────
        self._section_label(sb, "AUSWERTUNG")
        self._nav_btn(sb, "📊", "5. Ergebnisse", self._open_results,           ACCENT_GREEN)
        self._nav_btn(sb, "⚖",  "6. Vergleich", self._open_comparison_window,  ACCENT_YELLOW)

        # Admin (unten)
        tk.Frame(sb, bg=BG_PANEL).pack(fill="both", expand=True)
        ttk.Separator(sb, orient="horizontal").pack(fill="x", padx=12, pady=4)
        self._nav_btn(sb, "⚙", "Admin", self._open_admin, FG_MUTED)

        tk.Label(sb, text="v1.0", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 7)).pack(pady=(2, 8))

    def _nav_btn(self, parent, icon: str, label: str, cmd, color: str):
        frame = tk.Frame(parent, bg=BG_PANEL, cursor="hand2", height=46)
        frame.pack(fill="x")
        frame.pack_propagate(False)

        inner = tk.Frame(frame, bg=BG_PANEL)
        inner.place(relx=0.10, rely=0.5, anchor="w")
        icon_lbl = tk.Label(inner, text=icon, bg=BG_PANEL, fg=color,
                             font=("Segoe UI", 13))
        icon_lbl.pack(side="left")
        text_lbl = tk.Label(inner, text=label, bg=BG_PANEL, fg=FG_TEXT,
                             font=("Segoe UI", 9))
        text_lbl.pack(side="left", padx=(6, 0))

        all_widgets = (frame, inner, icon_lbl, text_lbl)

        def on_enter(_):
            for w in all_widgets:
                w.config(bg=BTN_ACTIVE)

        def on_leave(_):
            for w in all_widgets:
                w.config(bg=BG_PANEL)

        def on_click(_):
            cmd()

        for w in all_widgets:
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)

    def _section_label(self, parent, text: str):
        tk.Label(
            parent, text=text,
            bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 7, "bold"),
            anchor="w", padx=16,
        ).pack(fill="x", pady=(4, 2))

    def _build_dashboard(self, parent):
        dash = tk.Frame(parent, bg=BG_MAIN)
        dash.pack(fill="x", padx=16, pady=(16, 8))

        self._stat_cards: dict[str, tk.StringVar] = {}
        cards = [
            ("total",         "ISINs gesamt",  ACCENT_LAVENDER),
            ("analysiert",    "Analysiert",    ACCENT_GREEN),
            ("retail",        "Retail",        ACCENT_GREEN),
            ("institutional", "Institutional", ACCENT_BLUE),
            ("unklar",        "Unklar",        ACCENT_YELLOW),
        ]
        for key, label, color in cards:
            card = tk.Frame(dash, bg=BG_PANEL, padx=14, pady=10)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(card, text=label, bg=BG_PANEL, fg=FG_MUTED,
                     font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value="—")
            tk.Label(card, textvariable=var, bg=BG_PANEL, fg=color,
                     font=("Segoe UI", 18, "bold")).pack(anchor="w")
            self._stat_cards[key] = var

        self._refresh_dashboard()

    def _refresh_dashboard(self):
        try:
            import results_store
            s = results_store.get_stats()
            self._stat_cards["total"].set(str(s.get("total", 0)))
            analysiert = s.get("retail", 0) + s.get("institutional", 0)
            self._stat_cards["analysiert"].set(str(analysiert))
            self._stat_cards["retail"].set(str(s.get("retail", 0)))
            self._stat_cards["institutional"].set(str(s.get("institutional", 0)))
            self._stat_cards["unklar"].set(str(s.get("unklar", 0)))
        except Exception:
            pass
        self.after(5_000, self._refresh_dashboard)

    def _build_log_panel(self, parent):
        header = tk.Frame(parent, bg=BG_MAIN)
        header.pack(fill="x", padx=16, pady=(0, 4))

        tk.Label(
            header, text="📋 Aktivitäts-Log", bg=BG_MAIN, fg=ACCENT_BLUE,
            font=("Segoe UI", 10, "bold"), anchor="w"
        ).pack(side="left")

        tk.Button(
            header, text="🗑 Log leeren", command=self._clear_log,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2"
        ).pack(side="right")

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            parent, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.pack(fill="x", padx=16, pady=(0, 6))

        self.progress_label = tk.Label(
            parent, text="", bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8)
        )
        self.progress_label.pack(anchor="w", padx=16)

        self.log_text = scrolledtext.ScrolledText(
            parent, bg=BG_PANEL, fg=FG_TEXT,
            font=("Consolas", 9), relief="flat",
            state="disabled", wrap="word"
        )
        self.log_text.pack(fill="both", expand=True, padx=16, pady=(4, 10))

        self.log_text.tag_config("ok",     foreground=ACCENT_GREEN)
        self.log_text.tag_config("error",  foreground=ACCENT_RED)
        self.log_text.tag_config("warn",   foreground=ACCENT_YELLOW)
        self.log_text.tag_config("info",   foreground=ACCENT_BLUE)
        self.log_text.tag_config("muted",  foreground=FG_MUTED)
        self.log_text.tag_config("rule",   foreground=ACCENT_LAVENDER)
        self.log_text.tag_config("llm",    foreground="#cba6f7")
        self.log_text.tag_config("detail", foreground="#6c7086")

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="Bereit")
        tk.Label(
            self, textvariable=self.status_var,
            bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w", padx=10, pady=3
        ).pack(fill="x", side="bottom")

    # ─── Log ─────────────────────────────────────────────────────────

    def _log_tag(self, msg: str) -> str:
        if "❌" in msg:
            return "error"
        if "✅" in msg:
            return "ok"
        if "📐" in msg or "Regelextraktor" in msg or "Regelbasiert →" in msg:
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

    # ─── Event-Queue ─────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                event = self._progress_queue.get_nowait()
                msg = getattr(event, "message", "")
                tag = self._log_tag(msg)
                if event.type == "log":
                    self._log(msg, tag)
                elif event.type == "progress":
                    self._log(msg, tag)
                    total = getattr(event, "total", 0)
                    done = getattr(event, "done", 0)
                    if total > 0:
                        self.progress_var.set(done / total * 100)
                        self.progress_label.config(text=f"{done} / {total}")
                elif event.type == "error":
                    self._log(f"❌ {msg}", "error")
                    self.status_var.set(f"Fehler: {msg[:60]}")
                elif event.type == "done":
                    self._log(f"✅ {msg}", "ok")
                    self.progress_var.set(0)
                    self.progress_label.config(text="")
                    self.status_var.set(msg[:80])
                    self._refresh_dashboard()
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ─── Sub-Prozess-Status ──────────────────────────────────────────

    def notify_process(self, name: str, running: bool):
        """Sub-Fenster melden Start/Ende ihres Prozesses für die Statusleiste."""
        if running:
            self._running_processes.add(name)
        else:
            self._running_processes.discard(name)

        if self._running_processes:
            names = " · ".join(sorted(self._running_processes))
            self.status_var.set(f"⚙  Läuft: {names} …")
        else:
            self.status_var.set("Bereit")

    # ─── Ergebnis-DB ─────────────────────────────────────────────────

    def _store_result(self, isin: str, fondsname: str, result: dict, pdf_datei: str = ""):
        try:
            import results_store
            results_store.upsert_result(isin, fondsname, result, pdf_datei)
        except Exception:
            pass
        try:
            if self._results_win and self._results_win.winfo_exists():
                self._results_win.refresh()
        except Exception:
            pass
        try:
            if self._data_mgmt_win and self._data_mgmt_win.winfo_exists():
                self._data_mgmt_win.refresh_results()
        except Exception:
            pass

    # ─── Fenster öffnen ──────────────────────────────────────────────

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

    def _open_pdf_trim_window(self):
        from pdf_trim_window import PdfTrimWindow
        if self._pdf_trim_win and self._pdf_trim_win.winfo_exists():
            self._pdf_trim_win.lift()
            self._pdf_trim_win.focus_force()
        else:
            self._pdf_trim_win = PdfTrimWindow(self)

    def _open_comparison_window(self):
        from comparison_window import ComparisonWindow
        if self._comparison_win and self._comparison_win.winfo_exists():
            self._comparison_win.lift()
            self._comparison_win.focus_force()
        else:
            self._comparison_win = ComparisonWindow(self)

    def _open_admin(self):
        from admin_panel import AdminPanel
        AdminPanel(self)

    def _open_excel(self):
        excel_path = os.getenv("EXCEL_PATH", "")
        if excel_path and os.path.exists(excel_path):
            os.startfile(excel_path)
        else:
            messagebox.showinfo("Info", "Excel-Datei nicht gefunden. Bitte in Admin konfigurieren.")


def main():
    project_root = Path(__file__).parent.parent
    os.chdir(project_root)
    for d in ["data/input", "data/output", "data/prospectus"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
