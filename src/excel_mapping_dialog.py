"""Modaler Dialog: Excel-Spaltenköpfe auf DB-Felder mappen."""

import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk

# ─── Felder ───────────────────────────────────────────────────────────────────

# Anzeigename → DB-Spaltenname  (leer = ignorieren)
DB_FIELDS: dict[str, str] = {
    "— ignorieren —":      "",
    "ISIN (Pflicht)":      "isin",
    "Fondsname":           "fondsname",
    "Fund-ID":             "fund_id",
    "Prüf-Segmentierung":  "pruef_segmentierung",
    "Subfonds-Name":       "subfonds_name",
    "Umbrella-ID":         "umbrella_id",
    "Anteilsklasse":       "anteilsklasse",
    "Ausschüttungsart":    "ausschuettungsart",
    "Fondswährung":        "fondswaehrung",
    "TER":                 "ter",
    "Qualif. Anleger CH":  "qualif_anleger_ch",
    "Institutional CH":    "institutional_ch",
}

_DB_TO_LABEL: dict[str, str] = {v: k for k, v in DB_FIELDS.items() if v}
_FIELD_LABELS: list[str] = list(DB_FIELDS.keys())

# Automatische Erkennung: Schlüsselwort (normalisiert) → DB-Spalte
_AUTOMAP: list[tuple[str, str]] = [
    ("isin",           "isin"),
    ("groupinvestment","fondsname"),
    ("fondsname",      "fondsname"),
    ("fundname",       "fondsname"),
    ("name",           "fondsname"),
    ("morningstar",    "pruef_segmentierung"),
    ("msseg",          "pruef_segmentierung"),
    ("pruef",          "pruef_segmentierung"),
    ("segmentierung",  "pruef_segmentierung"),
    ("subfonds",       "subfonds_name"),
    ("subfund",        "subfonds_name"),
    ("umbrella",       "umbrella_id"),
    ("anteilsklasse",  "anteilsklasse"),
    ("shareclass",     "anteilsklasse"),
    ("klasse",         "anteilsklasse"),
    ("ausschuett",     "ausschuettungsart"),
    ("distribution",   "ausschuettungsart"),
    ("waehrung",       "fondswaehrung"),
    ("currency",       "fondswaehrung"),
    ("ter",            "ter"),
    ("fundid",         "fund_id"),
    ("qualif",         "qualif_anleger_ch"),
    ("institutional",  "institutional_ch"),
]

_MAPPING_FILE = Path(__file__).parent.parent / "data" / "output" / "excel_mapping.json"

# ─── Farben ───────────────────────────────────────────────────────────────────
BG_MAIN     = "#1e1e2e"
BG_PANEL    = "#2a2a3e"
BG_INPUT    = "#313145"
FG_TEXT     = "#cdd6f4"
FG_MUTED    = "#7f849c"
ACCENT_BLUE = "#89b4fa"
ACCENT_GREEN= "#a6e3a1"
ACCENT_RED  = "#f38ba8"
BTN_BG      = "#45475a"
BTN_ACTIVE  = "#585b70"


# ─── Persistenz ───────────────────────────────────────────────────────────────

def load_saved_mapping() -> dict[str, str]:
    """Lädt zuletzt gespeichertes Mapping {excel_header: db_field}."""
    try:
        if _MAPPING_FILE.exists():
            return json.loads(_MAPPING_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_mapping(mapping: dict[str, str]) -> None:
    """Speichert Mapping als JSON neben der DB."""
    try:
        _MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MAPPING_FILE.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ─── Auto-Erkennung ───────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return s.lower().replace(" ", "").replace("_", "").replace("-", "")


def auto_detect(header: str) -> str:
    """Gibt DB-Feld zurück, das am besten zum Excel-Spaltenkopf passt (oder '')."""
    h = _norm(header)
    for key, field in _AUTOMAP:
        k = _norm(key)
        if k == h or k in h or h in k:
            return field
    return ""


# ─── Dialog ───────────────────────────────────────────────────────────────────

class ExcelMappingDialog(tk.Toplevel):
    """
    Modaler Dialog: eine Zeile pro Excel-Spalte mit Dropdown für das Ziel-DB-Feld.

    Nach `wait_window()`:
      self.result  →  dict {excel_header: db_field}  oder  None (abgebrochen)
    """

    def __init__(
        self,
        parent: tk.Widget,
        headers: list[str],
        saved: dict[str, str],
    ):
        super().__init__(parent)
        self.title("Spalten-Mapping")
        self.configure(bg=BG_MAIN)
        self.resizable(False, True)
        self.grab_set()

        self.result: dict[str, str] | None = None
        self._headers = headers
        self._vars: list[tk.StringVar] = []

        self._build_ui(saved)
        self._center()

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px = self.master.winfo_rootx() + self.master.winfo_width() // 2
        py = self.master.winfo_rooty() + self.master.winfo_height() // 2
        self.geometry(f"{w}x{min(h, 640)}+{px - w // 2}+{py - min(h, 640) // 2}")

    def _build_ui(self, saved: dict[str, str]):
        # ── Kopfzeile ─────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=BG_PANEL)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="  Spalten-Mapping  —  Excel-Spalte  →  Datenbank-Feld",
            bg=BG_PANEL, fg=ACCENT_BLUE,
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(side="left", padx=12, pady=10)
        tk.Label(
            hdr,
            text=f"{len(self._headers)} Spalten erkannt",
            bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="e",
        ).pack(side="right", padx=12)

        # ── Scrollbarer Bereich ───────────────────────────────────────────────
        outer = tk.Frame(self, bg=BG_MAIN)
        outer.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        canvas = tk.Canvas(outer, bg=BG_MAIN, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG_MAIN)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        # Spaltenköpfe
        tk.Label(
            inner, text="Excel-Spalte", bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 9, "bold"), width=30, anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=(4, 2), sticky="w")
        tk.Label(
            inner, text="DB-Feld", bg=BG_MAIN, fg=FG_MUTED,
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).grid(row=0, column=1, padx=(4, 8), pady=(4, 2), sticky="w")
        ttk.Separator(inner, orient="horizontal").grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 4)
        )

        for i, header in enumerate(self._headers):
            display = header if header else f"(Spalte {i + 1})"
            saved_field = saved.get(header, "") or auto_detect(header)
            initial_label = _DB_TO_LABEL.get(saved_field, "— ignorieren —")

            tk.Label(
                inner, text=display, bg=BG_MAIN, fg=FG_TEXT,
                font=("Segoe UI", 9), anchor="w", width=30,
            ).grid(row=i + 2, column=0, padx=(8, 4), pady=2, sticky="w")

            var = tk.StringVar(value=initial_label)
            self._vars.append(var)
            ttk.Combobox(
                inner, textvariable=var, values=_FIELD_LABELS,
                state="readonly", font=("Segoe UI", 9), width=26,
            ).grid(row=i + 2, column=1, padx=(4, 8), pady=2, sticky="ew")

        inner.columnconfigure(1, weight=1)

        def _resize(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(win_id, width=event.width)

        inner.bind("<Configure>", _resize)

        def _wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>",  lambda _: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

        # Sinnvolle Höhe: max 22 Zeilen à 30 px
        canvas.configure(height=min(len(self._headers) + 2, 22) * 30)

        # ── Status + Buttons ──────────────────────────────────────────────────
        btn_bar = tk.Frame(self, bg=BG_MAIN)
        btn_bar.pack(fill="x", padx=12, pady=(4, 12))

        self._status_lbl = tk.Label(
            btn_bar, text="", bg=BG_MAIN, fg=ACCENT_RED, font=("Segoe UI", 8)
        )
        self._status_lbl.pack(side="left")

        tk.Button(
            btn_bar, text="  Abbrechen  ", command=self.destroy,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=5, cursor="hand2",
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_bar, text="  ✔  Importieren  ", command=self._confirm,
            bg="#1e3a1e", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9, "bold"), padx=10, pady=5, cursor="hand2",
        ).pack(side="right")

    def _confirm(self):
        mapping: dict[str, str] = {}
        for header, var in zip(self._headers, self._vars):
            db_field = DB_FIELDS.get(var.get(), "")
            if db_field and header:
                mapping[header] = db_field

        if "isin" not in mapping.values():
            self._status_lbl.config(text="⚠ Keine Spalte als 'ISIN (Pflicht)' zugeordnet!")
            return

        save_mapping(mapping)
        self.result = mapping
        self.destroy()
