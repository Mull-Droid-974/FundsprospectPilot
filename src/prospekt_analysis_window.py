"""
LLM-Prospekt-Analyse Fenster.

Analysiert Fondsprospekte per LLM: ein Aufruf pro Subfonds, klassifiziert
alle Anteilsklassen (ISINs) und schreibt fondstyp/anlegertyp/kundentyp/
llm_segmentierung/llm_segmentierung_begruendung in die DB.
"""

import os
import queue
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import results_store
import typologie_store
from llm_analysis_worker import AnalysisEvent, LLMAnalysisWorker

# Farben
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

# Verfügbare Modelle (identisch mit admin_panel.py)
MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5-20251001",
]

DEFAULT_PROMPT = """\
Du bist ein erfahrener Wertpapierrechtsspezialist mit umfassenden Kenntnissen des \
Schweizer Kollektivanlagerechts (KAG/CISA, FIDLEG), UCITS/OGAW-Richtlinien, AIFMD \
sowie MiFID II Anlegerklassifizierung.

Analysiere den nachfolgenden Verkaufsprospekt-Auszug und bestimme die nachfolgenden \
Felder. Wichtig: Verwende für FONDSTYP, ANLEGERTYP und KUNDENTYP AUSSCHLIESSLICH \
die erlaubten Werte aus den Listen — wähle den semantisch nächstliegenden Wert.

=== ERLAUBTE WERTE (zwingend auf diese mappen) ===

FONDSTYP:
{fondstyp_liste}

ANLEGERTYP:
{anlegertyp_liste}

KUNDENTYP (primärer, wichtigster Kundentyp der Anteilsklasse):
{kundentyp_liste}

=== AUFGABE 1 — Fondseigenschaften (Subfonds-Ebene) ===

Bestimme für den gesamten Subfonds:
- FONDSTYP: exakt ein Wert aus obiger Liste
- ANLEGERTYP: exakt ein Wert aus obiger Liste (primäre Zielgruppe)
- KUNDENTYP: exakt ein Wert aus obiger Liste (primärer Kundentyp)

Für jeden dieser drei Werte: gib zusätzlich den ROH-Wert an — d.h. die exakte \
Formulierung wie sie im Prospekt steht, bevor du sie auf die kanonische Liste gemappt hast. \
Füge am Anfang des Roh-Werts die PDF-Seitenzahl ein, auf der du die Information gefunden hast, \
im Format "S.<Nr>: <Text>" (z.B. "S.14: Exchange Traded Fund, passiv verwaltet").

=== AUFGABE 2 — Segmentierung pro Anteilsklasse ===

Für jede der nachfolgend aufgeführten ISINs/Anteilsklassen bestimme unabhängig \
die Investoren-Segmentierung.

SEGMENTIERUNGS-KATEGORIEN:
- retail: Privatanleger, öffentlicher Vertrieb, Minimum < 100'000 CHF/EUR
- institutional: Nur institutionelle/professionelle Anleger, Min. ≥ 500'000 CHF/EUR \
  oder explizite Einschränkung im Prospekt
- qualified: Qualifizierte Anleger (KAG Art.10/FIDLEG), Min. 100'000–499'999 CHF/EUR
- mixed: Keine klare Einschränkung, mehrere Anlegertypen adressiert
- unklar: Aus Prospektauszug nicht eindeutig bestimmbar

KLASSIFIZIERUNGS-HINWEISE:
- Klassen-Suffix I/Inst/Z/X/P → meist institutional
- Klassen-Suffix A/B/R/D/C → meist retail
- Prüfe explizit: Mindestzeichnung, "reserved for", "restricted to", TER-Höhe
- CH-ISINs oft KAG Art.10 qualifiziert; LU/IE-ISINs oft UCITS retail

BEKANNTE ISINs IN DIESEM FONDS:
{isin_list}

=== AUSGABE (NUR JSON, kein weiterer Text) ===
{
  "fondstyp":      "exakt ein Wert aus FONDSTYP-Liste",
  "fondstyp_roh":  "S.<Nr>: exakte Formulierung aus dem Prospekt vor dem Mapping",
  "anlegertyp":    "exakt ein Wert aus ANLEGERTYP-Liste",
  "anlegertyp_roh":"S.<Nr>: exakte Formulierung aus dem Prospekt vor dem Mapping",
  "kundentyp":     "exakt ein Wert aus KUNDENTYP-Liste",
  "kundentyp_roh": "S.<Nr>: exakte Formulierung aus dem Prospekt vor dem Mapping",
  "anteilsklassen": [
    {
      "isin":              "ISIN oder leer wenn nicht zuordenbar",
      "anteilsklasse_name":"Klassenname aus dem Prospekt",
      "segmentierung":     "retail|institutional|qualified|mixed|unklar",
      "begruendung":       "max. 200 Zeichen — warum diese Kategorie"
    }
  ]
}"""


class ProspektAnalysisWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("LLM-Prospekt-Analyse")
        self.configure(bg=BG_MAIN)
        self.geometry("920x700")
        self.minsize(700, 500)

        self._worker: LLMAnalysisWorker | None = None
        self._event_queue: queue.Queue = queue.Queue()
        self._prompt = DEFAULT_PROMPT
        self._subfonds_map: dict[str, list] = {}   # subfonds_id → rows
        self._umbrella_map: dict[str, list] = {}   # umbrella_id → rows

        self._build_ui()
        self._refresh_data()
        self._poll_queue()

    # ─── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=BG_PANEL)
        toolbar.pack(fill="x")
        inner = tk.Frame(toolbar, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        tk.Label(
            inner, text="🔬  LLM-Prospekt-Analyse",
            bg=BG_PANEL, fg=ACCENT_LAVENDER,
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")

        self.btn_stop = tk.Button(
            inner, text="⏹  Stopp",
            command=self._stop_worker,
            bg="#3a2000", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE, state="disabled",
        )
        self.btn_stop.pack(side="right", padx=(4, 0))

        tk.Button(
            inner, text="✏  Prompt bearbeiten",
            command=self._open_prompt_editor,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            inner, text="📋  Werte verwalten",
            command=self._open_typologie,
            bg=BTN_BG, fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="right", padx=(4, 0))

        # Modell-Auswahl
        tk.Label(
            inner, text="Modell:",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(side="right", padx=(12, 4))

        self._model_var = tk.StringVar(value=MODELS[0])
        ttk.Combobox(
            inner, textvariable=self._model_var,
            values=MODELS, state="readonly", width=28,
            font=("Segoe UI", 9),
        ).pack(side="right")

        # ── Auswahl-Frame ─────────────────────────────────────────────────────
        sel_frame = tk.LabelFrame(
            self, text="Auswahl", bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 9), bd=1, relief="flat",
        )
        sel_frame.pack(fill="x", padx=12, pady=(8, 4))

        sel_inner = tk.Frame(sel_frame, bg=BG_PANEL)
        sel_inner.pack(fill="x", padx=10, pady=8)

        self._sel_mode = tk.StringVar(value="all")

        # Alle nicht analysierten
        row0 = tk.Frame(sel_inner, bg=BG_PANEL)
        row0.pack(fill="x", pady=2)
        tk.Radiobutton(
            row0, text="Alle nicht analysierten",
            variable=self._sel_mode, value="all",
            bg=BG_PANEL, fg=FG_TEXT, selectcolor=BG_INPUT,
            activebackground=BG_PANEL, font=("Segoe UI", 9),
            command=self._on_mode_change,
        ).pack(side="left")
        self._lbl_all_count = tk.Label(
            row0, text="", bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        )
        self._lbl_all_count.pack(side="left", padx=(6, 0))

        # Umbrella auswählen
        row1 = tk.Frame(sel_inner, bg=BG_PANEL)
        row1.pack(fill="x", pady=2)
        tk.Radiobutton(
            row1, text="Umbrella-Fonds:",
            variable=self._sel_mode, value="umbrella",
            bg=BG_PANEL, fg=FG_TEXT, selectcolor=BG_INPUT,
            activebackground=BG_PANEL, font=("Segoe UI", 9),
            command=self._on_mode_change,
        ).pack(side="left")
        self._umbrella_var = tk.StringVar()
        self._umbrella_cb = ttk.Combobox(
            row1, textvariable=self._umbrella_var,
            state="readonly", width=50, font=("Segoe UI", 9),
        )
        self._umbrella_cb.pack(side="left", padx=(6, 0))
        self._umbrella_cb.bind("<<ComboboxSelected>>", lambda _: None)

        # Einzelner Subfonds
        row2 = tk.Frame(sel_inner, bg=BG_PANEL)
        row2.pack(fill="x", pady=2)
        tk.Radiobutton(
            row2, text="Einzelner Subfonds:",
            variable=self._sel_mode, value="subfonds",
            bg=BG_PANEL, fg=FG_TEXT, selectcolor=BG_INPUT,
            activebackground=BG_PANEL, font=("Segoe UI", 9),
            command=self._on_mode_change,
        ).pack(side="left")
        self._subfonds_var = tk.StringVar()
        self._subfonds_cb = ttk.Combobox(
            row2, textvariable=self._subfonds_var,
            state="readonly", width=50, font=("Segoe UI", 9),
        )
        self._subfonds_cb.pack(side="left", padx=(6, 0))

        # Start-Button
        btn_row = tk.Frame(sel_inner, bg=BG_PANEL)
        btn_row.pack(fill="x", pady=(8, 0))
        self.btn_start = tk.Button(
            btn_row, text="▶  Analyse starten",
            command=self._start_analysis,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9, "bold"), padx=14, pady=4, cursor="hand2",
            activebackground=BTN_ACTIVE,
        )
        self.btn_start.pack(side="left")

        # ── Fortschritt ───────────────────────────────────────────────────────
        prog_frame = tk.Frame(self, bg=BG_MAIN)
        prog_frame.pack(fill="x", padx=12, pady=(4, 2))

        self._prog_var = tk.DoubleVar(value=0)
        self._prog_bar = ttk.Progressbar(
            prog_frame, variable=self._prog_var, maximum=100, length=350,
        )
        self._prog_bar.pack(side="left", padx=(0, 10))

        self._status_var = tk.StringVar(value="Bereit")
        tk.Label(
            prog_frame, textvariable=self._status_var,
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(side="left")

        # ── Log ───────────────────────────────────────────────────────────────
        tk.Label(
            self, text="Log", bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", padx=12)

        self._log = scrolledtext.ScrolledText(
            self, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat",
        )
        self._log.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    # ─── Daten laden ──────────────────────────────────────────────────────────

    def _refresh_data(self):
        """Lädt Subfonds- und Umbrella-Gruppen aus der DB."""
        # Subfonds-Gruppen die ein PDF haben
        sfg = results_store.get_subfonds_groups()
        self._subfonds_map = {
            k: v for k, v in sfg.items()
            if k and any(
                r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists()
                for r in v
            )
        }

        # Umbrella-Gruppen
        ug = results_store.get_umbrella_groups()
        self._umbrella_map = {
            k: v for k, v in ug.items()
            if k  # leeren Schlüssel (kein umbrella) ausschliessen
        }

        # Nicht-analysierte Queue
        queue_rows = results_store.get_analysis_queue()
        n_subfonds = len({r.get("subfonds_id", r["isin"]) for r in queue_rows if r.get("subfonds_id")})
        n_single   = sum(1 for r in queue_rows if not r.get("subfonds_id"))
        total_groups = n_subfonds + n_single
        self._lbl_all_count.config(
            text=f"({total_groups} Subfonds-Gruppen, {len(queue_rows)} ISINs)"
        )

        # Umbrella-Dropdown befüllen
        umbrella_labels = []
        self._umbrella_label_map: dict[str, str] = {}
        for uid, rows in sorted(self._umbrella_map.items(),
                                 key=lambda x: x[0].lower()):
            name = rows[0].get("fondsname", uid) if rows else uid
            # Kürze auf ersten sinnvollen Teil
            short = name.split(" - ")[0][:60] if " - " in name else name[:60]
            label = f"{short} ({len(rows)} ISINs)"
            umbrella_labels.append(label)
            self._umbrella_label_map[label] = uid

        self._umbrella_cb.config(values=umbrella_labels)
        if umbrella_labels:
            self._umbrella_cb.set(umbrella_labels[0])

        # Subfonds-Dropdown befüllen
        sf_labels = []
        self._subfonds_label_map: dict[str, str] = {}
        for sfid, rows in sorted(self._subfonds_map.items(),
                                  key=lambda x: (x[1][0].get("subfonds_name") or x[0]).lower()):
            name = rows[0].get("subfonds_name") or rows[0].get("fondsname") or sfid
            short = name.split(" - ")[-1][:60]
            label = f"{short} ({len(rows)} ISINs)"
            sf_labels.append(label)
            self._subfonds_label_map[label] = sfid

        self._subfonds_cb.config(values=sf_labels)
        if sf_labels:
            self._subfonds_cb.set(sf_labels[0])

    def _on_mode_change(self):
        pass  # Dropdowns sind immer sichtbar; Radiobutton steuert Auswahl

    # ─── Analyse starten ─────────────────────────────────────────────────────

    def _build_groups_for_run(self) -> dict | None:
        """Baut die groups-Dict je nach gewähltem Modus auf."""
        mode = self._sel_mode.get()

        if mode == "all":
            queue_rows = results_store.get_analysis_queue()
            if not queue_rows:
                messagebox.showinfo(
                    "Keine ISINs",
                    "Alle ISINs mit Prospekt wurden bereits analysiert.",
                    parent=self,
                )
                return None
            # Gruppen aus Queue aufbauen
            groups: dict = {}
            for r in queue_rows:
                key = r.get("subfonds_id") or f"__single_{r['isin']}"
                groups.setdefault(key, []).append(r)
            return groups

        elif mode == "umbrella":
            label = self._umbrella_var.get()
            if not label:
                messagebox.showwarning("Keine Auswahl", "Bitte einen Umbrella-Fonds auswählen.", parent=self)
                return None
            uid = self._umbrella_label_map.get(label)
            if not uid:
                return None
            rows = self._umbrella_map.get(uid, [])
            # Nur Subfonds mit PDF
            groups = {}
            for r in rows:
                key = r.get("subfonds_id") or f"__single_{r['isin']}"
                groups.setdefault(key, []).append(r)
            valid_groups = {
                k: v for k, v in groups.items()
                if any(
                    rr.get("prospekt_pfad") and Path(rr["prospekt_pfad"]).exists()
                    for rr in v
                )
            }
            if not valid_groups:
                messagebox.showwarning(
                    "Kein PDF",
                    "Für diesen Umbrella-Fonds liegen keine PDFs vor.",
                    parent=self,
                )
                return None
            return valid_groups

        elif mode == "subfonds":
            label = self._subfonds_var.get()
            if not label:
                messagebox.showwarning("Keine Auswahl", "Bitte einen Subfonds auswählen.", parent=self)
                return None
            sfid = self._subfonds_label_map.get(label)
            if not sfid:
                return None
            rows = self._subfonds_map.get(sfid, [])
            return {sfid: rows} if rows else None

        return None

    def _build_prompt_with_taxonomy(self) -> str:
        """Injiziert aktuelle Taxonomie-Werte in den Prompt-Template."""
        fondstyp_liste  = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
        anlegertyp_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
        kundentyp_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
        return (
            self._prompt
            .replace("{fondstyp_liste}",   fondstyp_liste   or "  (keine Werte definiert)")
            .replace("{anlegertyp_liste}",  anlegertyp_liste or "  (keine Werte definiert)")
            .replace("{kundentyp_liste}",  kundentyp_liste  or "  (keine Werte definiert)")
        )

    def _open_typologie(self):
        from typologie_window import TypologieWindow
        TypologieWindow(self)

    def _start_analysis(self):
        if self._worker and self._worker.is_alive():
            return

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror(
                "API-Key fehlt",
                "Kein ANTHROPIC_API_KEY in .env gefunden.\n"
                "Bitte im Admin-Bereich konfigurieren.",
                parent=self,
            )
            return

        groups = self._build_groups_for_run()
        if not groups:
            return

        model = self._model_var.get()
        self._event_queue = queue.Queue()
        self._worker = LLMAnalysisWorker(
            groups=groups,
            prompt_template=self._build_prompt_with_taxonomy(),
            model=model,
            api_key=api_key,
            event_queue=self._event_queue,
        )
        self._worker.start()
        self._set_running(True)
        self._status_var.set(f"0 / {len(groups)} Gruppen")
        self._prog_var.set(0)
        self._log_line(f"Starte Analyse: {len(groups)} Subfonds-Gruppe(n), Modell: {model}")

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()

    def _set_running(self, running: bool):
        s_on  = "disabled" if running else "normal"
        s_off = "normal"   if running else "disabled"
        self.btn_start.config(state=s_on)
        self.btn_stop.config(state=s_off)

    # ─── Queue-Polling ────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                evt: AnalysisEvent = self._event_queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _handle_event(self, evt: AnalysisEvent):
        if evt.type == "log":
            self._log_line(f"[{evt.isin}] {evt.message}" if evt.isin else evt.message)

        elif evt.type == "progress":
            self._log_line(f"✓ [{evt.isin}] {evt.message}")
            if evt.total > 0:
                pct = (evt.done + evt.failed + evt.skipped) / evt.total * 100
                self._prog_var.set(pct)
                self._status_var.set(
                    f"{evt.done + evt.failed + evt.skipped}/{evt.total}  "
                    f"✓{evt.done}  ✗{evt.failed}  ⟳{evt.skipped}"
                )

        elif evt.type == "error":
            prefix = f"[{evt.isin}] " if evt.isin else ""
            self._log_line(f"✗ {prefix}{evt.message}")

        elif evt.type == "done":
            self._log_line(f"✅ {evt.message}")
            self._status_var.set(evt.message)
            self._prog_var.set(100)
            self._set_running(False)
            self._refresh_data()

    def _log_line(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")

    # ─── Prompt-Editor ────────────────────────────────────────────────────────

    def _open_prompt_editor(self):
        dlg = tk.Toplevel(self)
        dlg.title("Prompt bearbeiten")
        dlg.configure(bg=BG_MAIN)
        dlg.geometry("800x600")
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(
            dlg,
            text="LLM-Prompt (Platzhalter {isin_list} wird automatisch ersetzt)",
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(fill="x", padx=12, pady=(8, 2))

        txt = scrolledtext.ScrolledText(
            dlg, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 9), insertbackground=FG_TEXT, relief="flat",
            wrap="word",
        )
        txt.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        txt.insert("1.0", self._prompt)

        btn_frame = tk.Frame(dlg, bg=BG_MAIN)
        btn_frame.pack(fill="x", padx=12, pady=(0, 8))

        def _save():
            self._prompt = txt.get("1.0", "end-1c")
            dlg.destroy()

        def _reset():
            txt.delete("1.0", "end")
            txt.insert("1.0", DEFAULT_PROMPT)

        tk.Button(
            btn_frame, text="Speichern",
            command=_save,
            bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left")

        tk.Button(
            btn_frame, text="Zurücksetzen",
            command=_reset,
            bg=BTN_BG, fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left", padx=(6, 0))

        tk.Button(
            btn_frame, text="Abbrechen",
            command=dlg.destroy,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="right")
