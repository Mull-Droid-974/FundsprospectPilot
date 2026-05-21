"""
LLM-Prospekt-Analyse Fenster.

Zeigt alle Subfonds-Gruppen als Tabelle mit Status (ausstehend / analysiert /
teilweise / kein PDF). Batch-Start aller ausstehenden Gruppen oder Einzel-
Start per Auswahl / Doppelklick.
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
die erlaubten Werte aus den Listen — wähle den semantisch nächstliegenden Wert. \
ANLEGERTYP und KUNDENTYP werden pro Anteilsklasse bestimmt (nicht pro Subfonds).

=== ERLAUBTE WERTE (zwingend auf diese mappen) ===

FONDSTYP:
{fondstyp_liste}

ANLEGERTYP:
{anlegertyp_liste}

KUNDENTYP (primärer, wichtigster Kundentyp der Anteilsklasse):
{kundentyp_liste}

=== INFORMATIONSHIERARCHIE IM PROSPEKT ===

Fondsprospekte sind hierarchisch aufgebaut:
1. UMBRELLA-EBENE: Allgemeine Dachfonds-Informationen (Verwaltungsgesellschaft, Rechtsform, ...)
2. SUBFONDS-EBENE: Anlageziel, Benchmark, Risikoklasse — gilt für alle Anteilsklassen
3. ANTEILSKLASSEN-EBENE: Klassenspezifische Details (Mindestanlage, TER, Vertriebsbeschränkungen)

NICHT verwenden als anteilsklassen-spezifische Information:
- Allgemeine Abschnitte "Anteilsklassenkonzept" oder "Share Class Framework" \
(beschreiben mögliche Merkmale, keine konkreten Klassen-Parameter)
- Umbrella-weite Aussagen über Mindestzeiträume oder Mindestgrössen
- Formulierungen wie "der Fonds kann Klassen für..." oder "es sind Klassen möglich..."

=== AUFGABE 1 — Fondstyp (Subfonds-Ebene) ===

Bestimme für den gesamten Subfonds:
- FONDSTYP: exakt ein Wert aus obiger Liste
- fondstyp_roh: exakte Formulierung aus dem Prospekt mit Seitenangabe "S.<Nr>: <Text>"
- fondstyp_quelle: von welcher Hierarchie-Ebene die Information stammt

Suche primär auf SUBFONDS-Ebene (nicht Umbrella-Ebene).

=== AUFGABE 2 — Segmentierung pro Anteilsklasse ===

Für jede der nachfolgend aufgeführten ISINs/Anteilsklassen bestimme unabhängig \
die Investoren-Segmentierung.

SUCHSTRATEGIE (pro ISIN):
1. Suche den ISIN-Code wörtlich im Prospekttext (z.B. "CH0365696704")
2. Falls kein Treffer: Suche den Anteilsklassennamen (z.B. "Klasse I", "Class A") \
   in Tabellen oder Übersichten mit Mindestanlagebeträgen
3. Wenn weder ISIN noch Klassenname präzise gefunden: \
   setze info_quelle="nicht gefunden" und mindestanlage leer

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
- Anlegertyp-Beschränkungen auf ANTEILSKLASSEN-Ebene haben Vorrang vor SUBFONDS-Ebene
- Klassenname mit "I"/"Inst" ist ein Hinweis, kein Beweis — prüfe ob explizite Beschränkung vorhanden

ANLEGERTYP & KUNDENTYP (pro Anteilsklasse):
- Suche die ISIN oder den Klassennamen in einem Abschnitt, der beschreibt für welche
  Anleger diese spezifische Klasse bestimmt ist — NICHT einen allgemeinen Paragraphen
  der alle Klassen des Subfonds aufzählt.
- ANLEGERTYP: exakt ein Wert aus der ANLEGERTYP-Liste für DIESE Anteilsklasse (leer wenn nicht gefunden)
- ANLEGERTYP_ROH: exakter Wortlaut nur für diese Klasse, Format "S.<Nr>: <Text>"
- KUNDENTYP: exakt ein Wert aus der KUNDENTYP-Liste für DIESE Anteilsklasse (leer wenn nicht gefunden)
- KUNDENTYP_ROH: exakter Wortlaut nur für diese Klasse, Format "S.<Nr>: <Text>"

MINDESTANLAGE (pro Anteilsklasse):
- MINDESTANLAGE: Mindestzeichnungsbetrag exakt wie im Prospekt (Betrag + Währung, z.B. "10.000 CHF"). \
  Nur für diese Anteilsklasse — leer lassen wenn keine klassenspezifische Angabe gefunden.
- MINDESTANLAGE_ROH: Exakter Wortlaut aus dem Prospekt mit Seitenangabe, Format "S.<Nr>: <Text>"
- MINDESTANLAGE_QUELLE: Von welcher Hierarchie-Ebene stammt der Wert

BEKANNTE ISINs IN DIESEM FONDS:
{isin_list}

=== AUSGABE (NUR JSON, kein weiterer Text) ===
{
  "fondstyp":       "exakt ein Wert aus FONDSTYP-Liste",
  "fondstyp_roh":   "S.<Nr>: exakte Formulierung aus dem Prospekt vor dem Mapping",
  "fondstyp_quelle":"Subfonds|Umbrella|nicht gefunden",
  "anteilsklassen": [
    {
      "isin":                 "ISIN oder leer wenn nicht zuordenbar",
      "anteilsklasse_name":   "Klassenname aus dem Prospekt",
      "info_quelle":          "ISIN-spezifisch|Anteilsklasse|Subfonds|Umbrella|nicht gefunden",
      "anlegertyp":           "exakt ein Wert aus ANLEGERTYP-Liste (leer wenn nicht klassenspezifisch gefunden)",
      "anlegertyp_roh":       "S.<Nr>: exakter Wortlaut nur für diese Klasse",
      "kundentyp":            "exakt ein Wert aus KUNDENTYP-Liste (leer wenn nicht klassenspezifisch gefunden)",
      "kundentyp_roh":        "S.<Nr>: exakter Wortlaut nur für diese Klasse",
      "segmentierung":        "retail|institutional|qualified|mixed|unklar",
      "begruendung":          "max. 200 Zeichen — warum diese Kategorie",
      "mindestanlage":        "Betrag + Währung, z.B. '10.000 CHF' (leer wenn nicht gefunden)",
      "mindestanlage_roh":    "S.<Nr>: exakter Wortlaut aus dem Prospekt",
      "mindestanlage_quelle": "ISIN-spezifisch|Anteilsklasse|Subfonds|Umbrella|nicht gefunden"
    }
  ]
}"""

_STATUS_TEXT = {
    "pending": "⏳ Ausstehend",
    "done":    "✓ Analysiert",
    "partial": "⚠ Teilweise",
    "no_pdf":  "— Kein PDF",
}

_COLS = [
    ("pdf",       "PDF",                40, "center"),
    ("name",      "Subfonds / Name",   270, "w"),
    ("umbrella",  "Umbrella",          170, "w"),
    ("total",     "ISINs",              55, "center"),
    ("pending",   "Offen",              55, "center"),
    ("status",    "Status",            125, "w"),
    ("last_date", "Letzte Analyse",    140, "w"),
]


class _Tooltip:
    def __init__(self, widget, text: str):
        self._tip = None
        widget.bind("<Enter>", lambda e: self._show(e, text))
        widget.bind("<Leave>", lambda e: self._hide())

    def _show(self, event, text: str):
        x = event.widget.winfo_rootx() + 20
        y = event.widget.winfo_rooty() + event.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(event.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=text, justify="left",
            bg="#2e2e42", fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=6, wraplength=340,
        ).pack()

    def _hide(self):
        if self._tip:
            self._tip.destroy()
            self._tip = None


class ProspektAnalysisWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("LLM-Prospekt-Analyse")
        self.configure(bg=BG_MAIN)
        self.geometry("960x700")
        self.minsize(780, 520)

        self._worker: LLMAnalysisWorker | None = None
        self._event_queue: queue.Queue = queue.Queue()
        self._prompt = DEFAULT_PROMPT
        self._table_data: list[dict] = []
        self._batch_errors: list[str] = []

        self._build_ui()
        self._refresh_data()
        self._poll_queue()

    # ─── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()
        self._build_table_area()
        self._build_progress()
        self._build_log()

    def _build_toolbar(self):
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

        btn_reset = tk.Button(
            inner, text="⚠ Grosse PDFs zurücksetzen",
            command=self._reset_large_pdfs,
            bg="#2e1a00", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        )
        btn_reset.pack(side="right", padx=(4, 0))
        _Tooltip(btn_reset,
            "Setzt LLM-Analyseergebnisse zurück für ISINs, deren PDF mehr als\n"
            "2000 Seiten hat und noch nicht automatisch gekürzt wurde.\n"
            "Die Analyse kann danach erneut gestartet werden.")

        btn_prompt = tk.Button(
            inner, text="✏  Prompt",
            command=self._open_prompt_editor,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        )
        btn_prompt.pack(side="right", padx=(4, 0))
        _Tooltip(btn_prompt,
            "Öffnet den Prompt-Editor.\n"
            "Hier kann der LLM-Prompt angepasst werden, den das System zur\n"
            "Analyse der Prospekte verwendet. Änderungen gelten ab dem\n"
            "nächsten Analysestart. Mit 'Zurücksetzen' wird der Standard-\n"
            "Prompt wiederhergestellt.")

        btn_werte = tk.Button(
            inner, text="📋  Werte",
            command=self._open_typologie,
            bg=BTN_BG, fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        )
        btn_werte.pack(side="right", padx=(4, 0))
        _Tooltip(btn_werte,
            "Öffnet die Wertelisten (Typologie).\n"
            "Hier können die erlaubten Werte für Fondstyp, Anlegertyp und\n"
            "Kundentyp verwaltet werden. Diese Werte werden im LLM-Prompt\n"
            "als Vorgaben für das Mapping verwendet.")

        tk.Label(
            inner, text="Modell:",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(side="right", padx=(8, 4))

        self._model_var = tk.StringVar(value=MODELS[0])
        self._model_combo = ttk.Combobox(
            inner, textvariable=self._model_var,
            values=MODELS, state="readonly", width=28,
            font=("Segoe UI", 9),
        )
        self._model_combo.pack(side="right")

        tk.Label(
            inner, text="Anbieter:",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(side="right", padx=(16, 4))

        self._provider_var = tk.StringVar(value="anthropic")
        provider_combo = ttk.Combobox(
            inner, textvariable=self._provider_var,
            values=["anthropic", "gemini"], state="readonly", width=10,
            font=("Segoe UI", 9),
        )
        provider_combo.pack(side="right")
        provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)

        tk.Label(
            inner, text="Worker:",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(side="right", padx=(16, 4))

        self._workers_var = tk.IntVar(value=2)
        tk.Spinbox(
            inner, from_=1, to=4, textvariable=self._workers_var,
            width=3, font=("Segoe UI", 9),
            bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
            buttonbackground=BTN_BG, relief="flat",
        ).pack(side="right")

    def _on_provider_changed(self, _event=None):
        from llm_provider import get_models, get_default_batch_model
        provider = self._provider_var.get()
        models = get_models(provider)
        self._model_combo.config(values=models)
        self._model_var.set(get_default_batch_model(provider) if models else "")

    def _build_table_area(self):
        # ── Aktionszeile ──────────────────────────────────────────────────────
        ctrl = tk.Frame(self, bg=BG_MAIN)
        ctrl.pack(fill="x", padx=12, pady=(10, 4))

        self.btn_all = tk.Button(
            ctrl, text="▶  Alle ausstehenden",
            command=self._start_all_pending,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9, "bold"), padx=12, pady=3,
            cursor="hand2", activebackground=BTN_ACTIVE,
        )
        self.btn_all.pack(side="left")

        self.btn_sel = tk.Button(
            ctrl, text="▶  Ausgewählte starten",
            command=self._start_selected,
            bg="#1a1e2e", fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=12, pady=3,
            cursor="hand2", activebackground=BTN_ACTIVE,
        )
        self.btn_sel.pack(side="left", padx=(6, 0))

        tk.Button(
            ctrl, text="↺",
            command=self._refresh_data,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 9), padx=8, pady=3,
            cursor="hand2", activebackground=BTN_ACTIVE,
        ).pack(side="left", padx=(6, 0))

        self._filter_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            ctrl, text="nur ausstehende",
            variable=self._filter_var,
            command=self._fill_tree,
            bg=BG_MAIN, fg=FG_MUTED, selectcolor=BG_INPUT,
            activebackground=BG_MAIN, activeforeground=FG_TEXT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=(14, 0))

        tk.Label(
            ctrl, text="🔍", bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(14, 2))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._fill_tree())
        tk.Entry(
            ctrl, textvariable=self._search_var,
            bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", font=("Segoe UI", 9), width=20,
        ).pack(side="left")

        tk.Button(
            ctrl, text="✕",
            command=lambda: self._search_var.set(""),
            bg=BG_MAIN, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 8), padx=2, pady=1,
            cursor="hand2", activebackground=BG_MAIN,
        ).pack(side="left", padx=(2, 0))

        self._summary_var = tk.StringVar(value="")
        tk.Label(
            ctrl, textvariable=self._summary_var,
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8),
        ).pack(side="right")

        # ── Treeview ──────────────────────────────────────────────────────────
        tree_frame = tk.Frame(self, bg=BG_MAIN)
        tree_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        cols = [c[0] for c in _COLS]
        self._tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings",
            selectmode="extended",
        )

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
            background=BG_PANEL, foreground=FG_TEXT,
            fieldbackground=BG_PANEL, rowheight=26,
            font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
            background=BG_INPUT, foreground=ACCENT_BLUE,
            font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3a3a5e")])

        for col, header, width, anchor in _COLS:
            self._tree.heading(col, text=header,
                               command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=width, minwidth=40, anchor=anchor)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

        self._tree.tag_configure("pending",
            background="#272917", foreground=ACCENT_YELLOW)
        self._tree.tag_configure("partial",
            background="#18192c", foreground=ACCENT_BLUE)
        self._tree.tag_configure("done",
            background="#172317", foreground=ACCENT_GREEN)
        self._tree.tag_configure("no_pdf",
            background=BG_PANEL,  foreground=FG_MUTED)

        self._tree.bind("<Double-1>", self._on_double_click)

    def _build_progress(self):
        prog_frame = tk.Frame(self, bg=BG_MAIN)
        prog_frame.pack(fill="x", padx=12, pady=(2, 2))

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

    def _build_log(self):
        tk.Label(
            self, text="Log", bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", padx=12)

        self._log = scrolledtext.ScrolledText(
            self, height=7, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat",
        )
        self._log.pack(fill="x", padx=12, pady=(0, 8))

    # ─── Daten ────────────────────────────────────────────────────────────────

    def _refresh_data(self):
        all_rows = results_store.get_all_results()

        raw: dict[str, list] = {}
        for row in all_rows:
            key = row.get("subfonds_id") or f"__single_{row['isin']}"
            raw.setdefault(key, []).append(row)

        self._table_data = []
        for key, rows in raw.items():
            has_pdf = any(
                r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists()
                for r in rows
            )
            n_analyzed = sum(1 for r in rows if r.get("llm_segmentierung"))
            n_pending  = len(rows) - n_analyzed

            if not has_pdf:
                status = "no_pdf"
            elif n_pending == 0:
                status = "done"
            elif n_analyzed > 0:
                status = "partial"
            else:
                status = "pending"

            # Umbrella-Anzeigename aus fondsname ableiten
            umbrella_id = rows[0].get("umbrella_id", "")
            if umbrella_id:
                fn = rows[0].get("fondsname", "")
                umbrella_disp = fn.split(" - ")[0][:40] if " - " in fn else (fn[:40] or umbrella_id[:20])
            else:
                umbrella_disp = "—"

            # Subfonds-Anzeigename
            sf_name = (
                rows[0].get("subfonds_name")
                or rows[0].get("fondsname")
                or key
            )
            if " - " in sf_name:
                sf_name = sf_name.split(" - ", 1)[-1]

            # Letztes Analyse-Datum
            dates = [r.get("analysiert_am", "") for r in rows if r.get("analysiert_am")]
            last_date = max(dates) if dates else ""

            self._table_data.append({
                "key":       key,
                "rows":      rows,
                "name":      sf_name,
                "umbrella":  umbrella_disp,
                "total":     len(rows),
                "pending":   n_pending,
                "analyzed":  n_analyzed,
                "status":    status,
                "last_date": last_date,
                "has_pdf":   has_pdf,
            })

        # Zusammenfassung
        n_total   = len(self._table_data)
        n_done    = sum(1 for d in self._table_data if d["status"] == "done")
        n_pend    = sum(1 for d in self._table_data if d["status"] in ("pending", "partial"))
        n_no_pdf  = sum(1 for d in self._table_data if d["status"] == "no_pdf")
        self._summary_var.set(
            f"Gesamt: {n_total}  |  ✓ {n_done}  |  ⏳ {n_pend}  |  — {n_no_pdf} (kein PDF)"
        )

        self._fill_tree()

    def _fill_tree(self):
        self._tree.delete(*self._tree.get_children())
        only_pending = self._filter_var.get()
        search = self._search_var.get().strip().lower()

        # Sortierung: ausstehend zuerst, dann teilweise, dann analysiert, dann kein PDF
        order = {"pending": 0, "partial": 1, "done": 2, "no_pdf": 3}
        items = sorted(
            self._table_data,
            key=lambda d: (order[d["status"]], d["name"].lower()),
        )

        for item in items:
            if only_pending and item["status"] not in ("pending", "partial"):
                continue
            if search and not (
                search in item["name"].lower() or
                search in item["umbrella"].lower()
            ):
                continue
            pending_disp = str(item["pending"]) if item["has_pdf"] else "—"
            date_disp    = item["last_date"][:16] if item["last_date"] else "—"
            self._tree.insert("", "end", iid=item["key"], values=(
                "✓" if item["has_pdf"] else "✗",
                item["name"][:70],
                item["umbrella"],
                item["total"],
                pending_disp,
                _STATUS_TEXT[item["status"]],
                date_disp,
            ), tags=(item["status"],))

    def _sort_by(self, col: str):
        items = [(self._tree.set(iid, col), iid)
                 for iid in self._tree.get_children()]
        items.sort(key=lambda x: x[0].lower())
        for idx, (_, iid) in enumerate(items):
            self._tree.move(iid, "", idx)

    # ─── Analyse starten ──────────────────────────────────────────────────────

    def _start_all_pending(self):
        groups = {
            d["key"]: d["rows"]
            for d in self._table_data
            if d["status"] in ("pending", "partial") and d["has_pdf"]
        }
        if not groups:
            messagebox.showinfo(
                "Alle analysiert",
                "Alle Subfonds-Gruppen mit Prospekt wurden bereits analysiert.",
                parent=self,
            )
            return
        self._run_analysis(groups)

    def _start_selected(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showwarning(
                "Keine Auswahl",
                "Bitte mindestens eine Zeile in der Tabelle auswählen.",
                parent=self,
            )
            return
        groups: dict = {}
        no_pdf_names: list[str] = []
        for key in selected:
            item = next((d for d in self._table_data if d["key"] == key), None)
            if item:
                if item["has_pdf"]:
                    groups[key] = item["rows"]
                else:
                    no_pdf_names.append(item["name"])
        if not groups:
            messagebox.showwarning(
                "Kein PDF",
                "Keine der ausgewählten Gruppen hat einen Prospekt:\n"
                + ", ".join(no_pdf_names[:5]),
                parent=self,
            )
            return
        self._run_analysis(groups)

    def _on_double_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        item = next((d for d in self._table_data if d["key"] == iid), None)
        if not item:
            return
        if not item["has_pdf"]:
            messagebox.showinfo(
                "Kein PDF",
                f"Für '{item['name']}' liegt kein Prospekt vor.",
                parent=self,
            )
            return
        self._run_analysis({iid: item["rows"]})

    def _run_analysis(self, groups: dict):
        if self._worker and self._worker.is_alive():
            return
        self._batch_errors = []
        provider = self._provider_var.get()
        if provider == "gemini":
            api_key = os.getenv("GOOGLE_API_KEY", "")
            key_label = "GOOGLE_API_KEY"
        else:
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            key_label = "ANTHROPIC_API_KEY"
        if not api_key:
            messagebox.showerror(
                "API-Key fehlt",
                f"Kein {key_label} in .env gefunden.\n"
                "Bitte im Admin-Bereich konfigurieren.",
                parent=self,
            )
            return
        model = self._model_var.get()
        self._event_queue = queue.Queue()
        self._worker = LLMAnalysisWorker(
            groups=groups,
            prompt_template=self._build_prompt_with_taxonomy(),
            model=model,
            api_key=api_key,
            event_queue=self._event_queue,
            workers=self._workers_var.get(),
            provider=provider,
        )
        self._worker.start()
        self._set_running(True)
        self._status_var.set(f"0 / {len(groups)} Gruppen")
        self._prog_var.set(0)
        self._log_line(
            f"Starte Analyse: {len(groups)} Subfonds-Gruppe(n), Modell: {model}"
        )

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()

    def _reset_large_pdfs(self):
        import pdf_analyzer
        from tkinter import messagebox
        candidates = []
        for row in results_store.get_all_results():
            if not row.get("llm_segmentierung"):
                continue
            pdf_path = row.get("prospekt_pfad") or ""
            if not pdf_path or not Path(pdf_path).exists():
                continue
            if Path(pdf_path).with_suffix(".trimmed.txt").exists():
                continue
            pages = pdf_analyzer.get_pdf_metadata(pdf_path).get("pages", 0)
            if pages > pdf_analyzer._MAX_PAGES:
                candidates.append(row["isin"])

        if not candidates:
            messagebox.showinfo(
                "Keine Treffer",
                "Keine analysierten ISINs mit grossem PDF (ohne .trimmed.txt) gefunden.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Zurücksetzen bestätigen",
            f"{len(candidates)} ISIN(s) gefunden deren PDF > {pdf_analyzer._MAX_PAGES} Seiten hat "
            f"und noch kein .trimmed.txt besitzt.\n\nAnalyseergebnisse zurücksetzen?",
            parent=self,
        ):
            return

        results_store.reset_llm_analysis(candidates)
        self._log_line(f"↺ {len(candidates)} ISIN(s) zurückgesetzt — bitte PDF-Kürzer verwenden, dann erneut analysieren.")
        self._refresh_data()

    def _set_running(self, running: bool):
        s_on  = "disabled" if running else "normal"
        s_off = "normal"   if running else "disabled"
        self.btn_all.config(state=s_on)
        self.btn_sel.config(state=s_on)
        self.btn_stop.config(state=s_off)
        if hasattr(self.master, "notify_process"):
            self.master.notify_process("Analyse", running)

    # ─── Prompt ───────────────────────────────────────────────────────────────

    def _build_prompt_with_taxonomy(self) -> str:
        fondstyp_liste   = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
        anlegertyp_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
        kundentyp_liste  = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
        return (
            self._prompt
            .replace("{fondstyp_liste}",   fondstyp_liste   or "  (keine Werte definiert)")
            .replace("{anlegertyp_liste}",  anlegertyp_liste or "  (keine Werte definiert)")
            .replace("{kundentyp_liste}",  kundentyp_liste  or "  (keine Werte definiert)")
        )

    def _open_typologie(self):
        from typologie_window import TypologieWindow
        TypologieWindow(self)

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
            font=("Consolas", 9), insertbackground=FG_TEXT,
            relief="flat", wrap="word",
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

        tk.Button(btn_frame, text="Speichern", command=_save,
                  bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
                  font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left")
        tk.Button(btn_frame, text="Zurücksetzen", command=_reset,
                  bg=BTN_BG, fg=ACCENT_YELLOW, relief="flat",
                  font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left", padx=(6, 0))
        tk.Button(btn_frame, text="Abbrechen", command=dlg.destroy,
                  bg=BTN_BG, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="right")

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
            self._refresh_data()

        elif evt.type == "error":
            prefix = f"[{evt.isin}] " if evt.isin else ""
            self._log_line(f"✗ {prefix}{evt.message}")
            self._batch_errors.append(f"[{evt.isin or '?'}] {evt.message}")

        elif evt.type == "done":
            self._log_line("─" * 60)
            self._log_line(
                f"✅  Fertig — Analysiert: {evt.done}  |  Fehler: {evt.failed}  |  Übersprungen: {evt.skipped}"
            )
            if self._batch_errors:
                self._log_line("Fehlgeschlagene Gruppen:")
                for err in self._batch_errors:
                    self._log_line(f"  ✗ {err}")
            self._status_var.set(evt.message)
            self._prog_var.set(100)
            self._set_running(False)
            self._refresh_data()
            if self._batch_errors:
                messagebox.showwarning(
                    "Batch abgeschlossen — mit Fehlern",
                    f"Analysiert: {evt.done}  |  Fehler: {evt.failed}  |  Übersprungen: {evt.skipped}\n\n"
                    + "\n".join(self._batch_errors[:20])
                    + ("\n\n… und weitere" if len(self._batch_errors) > 20 else ""),
                    parent=self,
                )

    def _log_line(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")
