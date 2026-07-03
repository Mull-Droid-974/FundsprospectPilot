"""
LLM-Prospekt-Analyse Fenster.

Zeigt alle Subfonds-Gruppen als Tabelle mit Status (ausstehend / analysiert /
teilweise / kein PDF). Batch-Start aller ausstehenden Gruppen oder Einzel-
Start per Auswahl / Doppelklick.
"""

import os
import queue
import sys
import threading
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

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

# Analyse-Modelle: Sonnet 4.6 (Standard) und Opus 4.8. Kein Haiku (zu schwache
# Klassifizierung). Kostenersparnis kommt aus der Batch-API (−50%) + nativem PDF
# mit Seiten-Reduktion großer Dateien (kein separater Trim-Modell-Schritt mehr).
MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-8",
]

DEFAULT_PROMPT = """\
Du bist ein erfahrener Wertpapierrechtsspezialist mit umfassenden Kenntnissen des \
Schweizer Kollektivanlagerechts (KAG/CISA, FIDLEG), UCITS/OGAW-Richtlinien, AIFMD \
sowie MiFID II Anlegerklassifizierung.

Analysiere den nachfolgenden Verkaufsprospekt-Auszug und bestimme die nachfolgenden \
Felder. Wichtig: Verwende für FONDSTYP, ANLEGERTYP, KUNDENTYP, DIENSTLEISTUNG und \
VERTRIEBSKANAL AUSSCHLIESSLICH die erlaubten Werte aus den Listen — wähle den \
semantisch nächstliegenden Wert. \
ANLEGERTYP, KUNDENTYP, DIENSTLEISTUNG und VERTRIEBSKANAL werden pro Anteilsklasse \
bestimmt (nicht pro Subfonds).

=== ERLAUBTE WERTE (zwingend auf diese mappen) ===

FONDSTYP:
{fondstyp_liste}

ANLEGERTYP:
{anlegertyp_liste}

KUNDENTYP (primärer, wichtigster Kundentyp der Anteilsklasse):
{kundentyp_liste}

DIENSTLEISTUNG (Dienstleistungsart pro Anteilsklasse):
{dienstleistung_liste}

VERTRIEBSKANAL (primärer Vertriebskanal pro Anteilsklasse):
{vertriebskanal_liste}

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

DIENSTLEISTUNG (pro Anteilsklasse — welche Vertriebsdienstleistung adressiert die Klasse):
- Delegation: Klasse für diskretionäre Vermögensverwaltung / Verwaltungsmandat (DPM). \
  Signale: "discretionary mandate", "Verwaltungsmandat", "im Rahmen eines Mandats", \
  oft institutionelle/clean Klassen ohne Retrozession.
- Beratung: Klasse für Anlageberatung. Signale: "advisory", "Anlageberatung", \
  "im Rahmen einer Beratung", "advisory mandate".
- Execution only: Klasse für beratungsfreien Direktvertrieb. Signale: "execution only", \
  "ohne Beratung", "reine Ausführung".
- DIENSTLEISTUNG: exakt ein Wert aus der DIENSTLEISTUNG-Liste für DIESE Anteilsklasse (leer wenn nicht gefunden)
- DIENSTLEISTUNG_ROH: exakter Wortlaut nur für diese Klasse, Format "S.<Nr>: <Text>"

VERTRIEBSKANAL (pro Anteilsklasse — über welchen Kanal die Klasse vertrieben wird):
- Captive Channel: Vertrieb über konzerneigenen/hauseigenen Kanal. \
  Signale: "captive", "hausintern", "gruppenintern", "über die Vertriebsstellen des Initiators".
- Finanzintermediäre: Vertrieb über externe Banken/Distributoren. \
  Signale: "financial intermediaries", "Vertriebspartner", "distributing banks", "über Intermediäre".
- Vermittler: Vertrieb über Broker/Makler/Agenten. \
  Signale: "broker", "Makler", "tied agent", "Vermittler".
- VERTRIEBSKANAL: exakt ein Wert aus der VERTRIEBSKANAL-Liste für DIESE Anteilsklasse (leer wenn nicht gefunden)
- VERTRIEBSKANAL_ROH: exakter Wortlaut nur für diese Klasse, Format "S.<Nr>: <Text>"

WICHTIG zu DIENSTLEISTUNG und VERTRIEBSKANAL: Diese beiden Felder sind in vielen Prospekten \
NICHT explizit angegeben. Setze den Wert NUR wenn ein klarer textlicher Beleg für DIESE \
Anteilsklasse existiert. Im Zweifel leer lassen — NICHT aus dem Klassennamen oder der TER ableiten/raten.

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
      "dienstleistung":       "exakt ein Wert aus DIENSTLEISTUNG-Liste (leer wenn nicht gefunden)",
      "dienstleistung_roh":   "S.<Nr>: exakter Wortlaut nur für diese Klasse",
      "vertriebskanal":       "exakt ein Wert aus VERTRIEBSKANAL-Liste (leer wenn nicht gefunden)",
      "vertriebskanal_roh":   "S.<Nr>: exakter Wortlaut nur für diese Klasse",
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
        self._sort_col: str = "status"
        self._sort_asc: bool = True

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

        _init_provider = os.getenv("LLM_PROVIDER", "anthropic")
        from llm_provider import DEFAULT_SINGLE_MODELS
        _init_models = self._analysis_models(_init_provider)
        _init_default = DEFAULT_SINGLE_MODELS.get(_init_provider, _init_models[0] if _init_models else "")
        self._model_var = tk.StringVar(value=_init_default)
        self._model_combo = ttk.Combobox(
            inner, textvariable=self._model_var,
            values=_init_models, state="readonly", width=28,
            font=("Segoe UI", 9),
        )
        self._model_combo.pack(side="right")

        tk.Label(
            inner, text="Anbieter:",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(side="right", padx=(16, 4))

        # Anthropic-only (natives PDF) — kein Provider-/Trim-Wechsel mehr
        self._provider_var = tk.StringVar(value="anthropic")
        tk.Label(
            inner, text="Anthropic · nativ",
            bg=BG_PANEL, fg=ACCENT_GREEN, font=("Segoe UI", 9, "bold"),
        ).pack(side="right")

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

    @staticmethod
    def _analysis_models(provider: str) -> list[str]:
        """Modelle für die Analyse (Sonnet/Opus). Kein separates, schwächeres
        Batch-/Trim-Modell mehr — natives PDF + Seiten-Reduktion ersetzen das Trimmen."""
        from llm_provider import get_models
        return list(get_models(provider))

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

        # ── Zweite Aktionszeile: Batch-API (−50%, asynchron) ──────────────────
        ctrl2 = tk.Frame(self, bg=BG_MAIN)
        ctrl2.pack(fill="x", padx=12, pady=(0, 4))

        tk.Label(
            ctrl2, text="Batch-API (−50%, asynchron):",
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 6))

        self.btn_batch = tk.Button(
            ctrl2, text="📦  Batch starten (−50%)",
            command=self._start_batch_pending,
            bg="#2e2a1a", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=12, pady=3,
            cursor="hand2", activebackground=BTN_ACTIVE,
        )
        self.btn_batch.pack(side="left")

        self.btn_batch_fetch = tk.Button(
            ctrl2, text="📥  Batch abholen",
            command=self._fetch_batch_results,
            bg="#1a1e2e", fg=ACCENT_LAVENDER, relief="flat",
            font=("Segoe UI", 9), padx=12, pady=3,
            cursor="hand2", activebackground=BTN_ACTIVE,
        )
        self.btn_batch_fetch.pack(side="left", padx=(6, 0))

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
        self._tree.bind("<ButtonRelease-1>", self._on_single_click)

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

        # Umbrella einheitlich aus Morningstar-Branding (identisch zur Struktur-Ansicht)
        try:
            import morningstar_store
            ms_umb = morningstar_store.get_umbrella_map()
        except Exception:
            ms_umb = {}

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

            # Umbrella = Morningstar-Branding (most-common je Subfonds), identisch zur
            # Struktur-Ansicht; Fallback auf Fondsname-Heuristik, wenn kein Branding da ist
            brandings = [ms_umb[r["isin"]] for r in rows if ms_umb.get(r["isin"])]
            if brandings:
                umbrella_disp = Counter(brandings).most_common(1)[0][0]
            else:
                fn = rows[0].get("fondsname", "")
                umbrella_disp = (fn.split(" - ")[0][:40] if " - " in fn else fn[:40]) or "—"

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
                "last_date":   last_date,
                "has_pdf":     has_pdf,
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

        self._apply_sort()

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._apply_sort()

    def _apply_sort(self):
        col = self._sort_col
        if not col:
            return
        _num_cols = {"total", "pending"}

        def _key(val: str):
            if col in _num_cols:
                try:
                    return (0, int(val))
                except ValueError:
                    return (1, 0)
            return (0, val.lower())

        items = [(self._tree.set(iid, col), iid)
                 for iid in self._tree.get_children()]
        items.sort(key=lambda x: _key(x[0]), reverse=not self._sort_asc)
        for idx, (_, iid) in enumerate(items):
            self._tree.move(iid, "", idx)

        arrow = " ▲" if self._sort_asc else " ▼"
        for c, header, _, _ in _COLS:
            self._tree.heading(c, text=(header + arrow) if c == col else header)

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

    def _on_single_click(self, event):
        if self._tree.identify_column(event.x) != "#1":
            return
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        item = next((d for d in self._table_data if d["key"] == iid), None)
        if not item or not item["has_pdf"]:
            return
        pdf_path = next(
            (r["prospekt_pfad"] for r in item["rows"]
             if r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists()),
            None,
        )
        if pdf_path:
            os.startfile(pdf_path)

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
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror(
                "API-Key fehlt",
                "Kein ANTHROPIC_API_KEY in .env gefunden.\n"
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

    # ─── Batch-API (−50%, asynchron) ──────────────────────────────────────────

    def _start_batch_pending(self):
        provider = self._provider_var.get()
        if provider != "anthropic":
            messagebox.showinfo(
                "Nur Anthropic",
                "Die Batch-API (−50%) ist nur für den Anbieter 'anthropic' verfügbar.\n"
                "Bitte Anbieter im Admin auf Anthropic stellen.",
                parent=self,
            )
            return
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror("API-Key fehlt", "Kein ANTHROPIC_API_KEY in .env.", parent=self)
            return

        groups = {
            d["key"]: d["rows"]
            for d in self._table_data
            if d["status"] in ("pending", "partial") and d["has_pdf"]
        }
        if not groups:
            messagebox.showinfo("Nichts zu tun",
                                "Keine ausstehenden Subfonds-Gruppen mit Prospekt.",
                                parent=self)
            return

        _CHUNK = 10  # in 10er-Blöcken einreichen → erste Batches gehen sofort raus
        if not messagebox.askyesno(
            "Batch starten",
            f"{len(groups)} Gruppe(n) per Batch-API analysieren (−50% Kosten).\n\n"
            f"Wird in {_CHUNK}er-Blöcken eingereicht — die ersten Batches gehen sofort "
            "raus, der Rest folgt während der Vorbereitung. Abbruchsicher.\n\n"
            "Ergebnisse kommen asynchron (meist Minuten, max. 24h) — dann mit dem "
            "Button 'Batch abholen' speichern.\n\nFortfahren?",
            parent=self,
        ):
            return

        model = self._model_var.get()
        prompt = self._build_prompt_with_taxonomy()
        self._set_running(True)
        self._log_line(f"📦 Batch-Vorbereitung: {len(groups)} Gruppe(n) in {_CHUNK}er-Blöcken, Modell {model} …")

        def run():
            import batch_analysis

            def prog(done, total, msg):
                self.after(0, lambda: self._log_line(f"  [{done}/{total}] {msg}"))

            try:
                batch_ids = batch_analysis.submit_batch(
                    groups=groups, prompt_template=prompt, model=model,
                    api_key=api_key,
                    provider="anthropic", progress=prog, chunk_size=_CHUNK,
                )
                self.after(0, lambda: self._on_batch_submitted(batch_ids))
            except Exception as e:
                self.after(0, lambda: (self._log_line(f"✗ Batch-Fehler: {e}"),
                                       self._set_running(False),
                                       messagebox.showerror("Batch-Fehler", str(e), parent=self)))

        threading.Thread(target=run, daemon=True).start()

    def _on_batch_submitted(self, batch_ids: list):
        self._set_running(False)
        self._log_line(f"✓ {len(batch_ids)} Batch-Block/Blöcke eingereicht.")
        messagebox.showinfo(
            "Batches eingereicht",
            f"{len(batch_ids)} Block/Blöcke eingereicht.\n\n"
            "Ergebnisse später mit dem Button 'Batch abholen' speichern "
            "(holt automatisch alle fertigen Blöcke).",
            parent=self,
        )

    def _fetch_batch_results(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror("API-Key fehlt", "Kein ANTHROPIC_API_KEY in .env.", parent=self)
            return

        import batch_analysis
        batches = batch_analysis.list_local_batches()
        if not batches:
            messagebox.showinfo(
                "Keine Batches",
                "Keine lokal gespeicherten Batches gefunden.\n\n"
                "Hinweis: Eine Batch-ID entsteht erst, wenn im Log "
                "'✓ … Block eingereicht' steht. Während der Vorbereitung "
                "(Vorbereitung x/y) gibt es noch nichts abzuholen.",
                parent=self)
            return

        self._set_running(True)
        self._log_line(f"📥 Prüfe {len(batches)} lokale(n) Batch-Block/Blöcke …")

        def run():
            def prog(msg):
                self.after(0, lambda: self._log_line(f"   {msg}"))
            try:
                res = batch_analysis.fetch_all_pending(api_key, progress=prog)
                self.after(0, lambda: self._on_batch_fetched(res))
            except Exception as e:
                self.after(0, lambda: (self._log_line(f"✗ Abhol-Fehler: {e}"),
                                       self._set_running(False),
                                       messagebox.showerror("Fehler", str(e), parent=self)))

        threading.Thread(target=run, daemon=True).start()

    def _on_batch_fetched(self, res: dict):
        self._set_running(False)
        self._log_line(
            f"✓ Abgeholt: {res['saved']} ISIN gespeichert, "
            f"{res['ended']} Block/Blöcke fertig, {res['pending']} noch in Arbeit, "
            f"{res['errored']} Fehler")
        self._refresh_data()
        messagebox.showinfo(
            "Batch abgeholt",
            f"Gespeichert: {res['saved']} ISIN\n"
            f"Fertige Blöcke: {res['ended']}\n"
            f"Noch in Arbeit: {res['pending']}\n"
            f"Fehler: {res['errored']}\n\n"
            "Noch nicht fertige Blöcke später erneut abholen.",
            parent=self,
        )

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
        fondstyp_liste       = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("fondstyp"))
        anlegertyp_liste     = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("anlegertyp"))
        kundentyp_liste      = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("kundentyp"))
        dienstleistung_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("dienstleistung"))
        vertriebskanal_liste = "\n".join(f"  - {w}" for w in typologie_store.get_wert_liste("vertriebskanal"))
        return (
            self._prompt
            .replace("{fondstyp_liste}",       fondstyp_liste       or "  (keine Werte definiert)")
            .replace("{anlegertyp_liste}",      anlegertyp_liste     or "  (keine Werte definiert)")
            .replace("{kundentyp_liste}",       kundentyp_liste      or "  (keine Werte definiert)")
            .replace("{dienstleistung_liste}",  dienstleistung_liste or "  (keine Werte definiert)")
            .replace("{vertriebskanal_liste}",  vertriebskanal_liste or "  (keine Werte definiert)")
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
