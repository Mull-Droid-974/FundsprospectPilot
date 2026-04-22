"""Analyse-Pipeline-Fenster: zeigt Schritt-für-Schritt-Fortschritt der Einzel-PDF-Analyse."""

import tkinter as tk
from tkinter import ttk

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

_STEPS = [
    ("pdf",    "PDF extrahieren"),
    ("rules",  "Regelextraktor"),
    ("llm",    "Claude-Klassifizierung"),
    ("result", "Ergebnis"),
]


def _step_key(msg: str) -> str:
    if "PDF" in msg or "extrahier" in msg.lower() or "✂" in msg or "⤵" in msg:
        return "pdf"
    if "📐" in msg or "Regelextraktor" in msg or "Regelbasiert" in msg:
        return "rules"
    if "🤖" in msg or "LLM" in msg or "Claude" in msg:
        return "llm"
    if "✅" in msg or "Ergebnis" in msg:
        return "result"
    return ""


class AnalysisWorkflowWindow(tk.Toplevel):
    """
    Zeigt den Fortschritt der Einzel-PDF-Analyse als Pipeline-Ansicht.
    Wird von app.py geöffnet; bleibt nach Abschluss für Ergebnis-Anzeige offen.
    """

    def __init__(self, parent: tk.Widget, pdf_name: str, isin: str, fund_name: str):
        super().__init__(parent)
        self.title("Analyse-Pipeline")
        self.configure(bg=BG_MAIN)
        self.geometry("480x520")
        self.resizable(False, False)

        self._active_step: str = ""
        self._done_steps: set = set()
        self._step_labels: dict = {}
        self._step_icons: dict = {}
        self._log_lines: list[str] = []

        self._build_ui(pdf_name, isin, fund_name)

    def _build_ui(self, pdf_name: str, isin: str, fund_name: str):
        # Header
        hdr = tk.Frame(self, bg=BG_PANEL, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🔍  Analyse läuft",
                 bg=BG_PANEL, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 13, "bold")).pack()
        tk.Label(hdr, text=pdf_name,
                 bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack()
        if isin or fund_name:
            tk.Label(hdr, text=f"{isin}  {fund_name}".strip(),
                     bg=BG_PANEL, fg=FG_MUTED,
                     font=("Segoe UI", 8)).pack()

        # Pipeline-Schritte
        steps_frame = tk.Frame(self, bg=BG_MAIN)
        steps_frame.pack(fill="x", padx=24, pady=16)

        for key, label in _STEPS:
            row = tk.Frame(steps_frame, bg=BG_MAIN)
            row.pack(fill="x", pady=4)

            icon_lbl = tk.Label(row, text="○", bg=BG_MAIN, fg=FG_MUTED,
                                font=("Segoe UI", 14, "bold"), width=3)
            icon_lbl.pack(side="left")

            txt_lbl = tk.Label(row, text=label, bg=BG_MAIN, fg=FG_MUTED,
                               font=("Segoe UI", 10), anchor="w")
            txt_lbl.pack(side="left", padx=(6, 0))

            self._step_icons[key] = icon_lbl
            self._step_labels[key] = txt_lbl

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=20, pady=(4, 0))

        # Log-Bereich
        tk.Label(self, text="Details", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 8, "bold"), anchor="w"
                 ).pack(anchor="w", padx=20, pady=(6, 2))

        self._log = tk.Text(
            self, bg=BG_PANEL, fg=FG_TEXT,
            font=("Consolas", 8), relief="flat",
            state="disabled", wrap="word", height=10
        )
        self._log.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self._log.tag_config("ok", foreground=ACCENT_GREEN)
        self._log.tag_config("error", foreground=ACCENT_RED)
        self._log.tag_config("muted", foreground=FG_MUTED)
        self._log.tag_config("llm", foreground="#cba6f7")

        # Ergebnis-Bereich (zunächst leer)
        self._result_frame = tk.Frame(self, bg=BG_MAIN)
        self._result_frame.pack(fill="x", padx=20, pady=(0, 12))

        tk.Button(
            self, text="Schliessen", command=self.destroy,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2"
        ).pack(pady=(0, 12))

    def handle_message(self, msg: str):
        """Verarbeitet eine Log-Nachricht und aktualisiert die Pipeline-Anzeige."""
        key = _step_key(msg)
        if key and key not in self._done_steps:
            if self._active_step and self._active_step != key:
                self._mark_done(self._active_step)
            self._active_step = key
            self._mark_active(key)

        tag = "error" if "❌" in msg else ("ok" if "✅" in msg else ("llm" if "🤖" in msg else "muted"))
        self._append_log(msg.strip(), tag)

    def _mark_active(self, key: str):
        lbl = self._step_labels.get(key)
        icon = self._step_icons.get(key)
        if lbl:
            lbl.config(fg=ACCENT_BLUE, font=("Segoe UI", 10, "bold"))
        if icon:
            icon.config(text="▶", fg=ACCENT_BLUE)

    def _mark_done(self, key: str):
        if key in self._done_steps:
            return
        self._done_steps.add(key)
        lbl = self._step_labels.get(key)
        icon = self._step_icons.get(key)
        if lbl:
            lbl.config(fg=ACCENT_GREEN, font=("Segoe UI", 10))
        if icon:
            icon.config(text="✓", fg=ACCENT_GREEN)

    def _mark_error(self, key: str):
        lbl = self._step_labels.get(key)
        icon = self._step_icons.get(key)
        if lbl:
            lbl.config(fg=ACCENT_RED)
        if icon:
            icon.config(text="✘", fg=ACCENT_RED)

    def _append_log(self, text: str, tag: str = ""):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")

    def show_result(self, result: dict):
        """Zeigt das Klassifizierungsergebnis im Fenster an."""
        # Alle Schritte als erledigt markieren
        for key, _ in _STEPS:
            self._mark_done(key)

        seg = result.get("segmentierung", "unklar")
        konfidenz = result.get("konfidenz", "")

        seg_color = {
            "institutional": ACCENT_BLUE,
            "retail": ACCENT_GREEN,
        }.get(seg, ACCENT_YELLOW)

        # Alten Inhalt löschen
        for w in self._result_frame.winfo_children():
            w.destroy()

        outer = tk.Frame(self._result_frame, bg=BG_PANEL)
        outer.pack(fill="x")

        tk.Label(outer, text="Ergebnis", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 8, "bold"), anchor="w"
                 ).pack(anchor="w", padx=8, pady=(6, 2))

        fields = [
            ("Segmentierung", seg.upper(), seg_color),
            ("Fondstyp", result.get("fondstyp", "-"), FG_TEXT),
            ("Anlegertyp", result.get("anlegertyp", "-"), FG_TEXT),
            ("Konfidenz", konfidenz, ACCENT_YELLOW if konfidenz == "niedrig" else FG_TEXT),
        ]
        for label, value, color in fields:
            row = tk.Frame(outer, bg=BG_PANEL)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text=f"{label}:", bg=BG_PANEL, fg=FG_MUTED,
                     font=("Segoe UI", 8), width=14, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=BG_PANEL, fg=color,
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(side="left")

        beg = result.get("begruendung", "")
        if beg:
            tk.Label(outer, text=f"💬 {beg}", bg=BG_PANEL, fg=FG_MUTED,
                     font=("Segoe UI", 8), wraplength=420, justify="left", anchor="w",
                     pady=4
                     ).pack(anchor="w", padx=8, pady=(2, 6))
