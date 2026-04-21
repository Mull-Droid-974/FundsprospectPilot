"""
Prospekt-spezifische DB-Funktionen für den standalone FundProspektDownloader.

Liest/schreibt die Spalten prospekt_pfad und prospekt_url in die fund_results-Tabelle.
Der DB-Pfad wird über set_db_path() oder die Umgebungsvariable DB_PATH konfiguriert.
"""

import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent.parent / "FundProspektPilot" / "data" / "output" / "results.db"


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = Path(path)


def _connect() -> sqlite3.Connection:
    if not _DB_PATH.exists():
        raise FileNotFoundError(
            f"Datenbank nicht gefunden: {_DB_PATH}\n"
            "Bitte DB_PATH in .env konfigurieren."
        )
    con = sqlite3.connect(str(_DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _ensure_columns():
    """Fügt prospekt_pfad/url Spalten hinzu falls nicht vorhanden."""
    with _connect() as con:
        for col_def in [
            "prospekt_pfad TEXT DEFAULT ''",
            "prospekt_url TEXT DEFAULT ''",
            "subfonds_id TEXT DEFAULT ''",
            "subfonds_name TEXT DEFAULT ''",
            "umbrella_id TEXT DEFAULT ''",
            "anteilsklasse TEXT DEFAULT ''",
            "ausschuettungsart TEXT DEFAULT ''",
            "fondswaehrung TEXT DEFAULT ''",
            "fundinfo_ter TEXT DEFAULT ''",
        ]:
            try:
                con.execute(f"ALTER TABLE fund_results ADD COLUMN {col_def}")
            except Exception:
                pass


def get_all_results() -> list[dict]:
    _ensure_columns()
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM fund_results ORDER BY analysiert_am DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_result(isin: str) -> Optional[dict]:
    _ensure_columns()
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM fund_results WHERE isin = ?", (isin,)
        ).fetchone()
    return dict(row) if row else None


def update_fundinfo_meta(
    isin: str,
    subfonds_id: str = "",
    subfonds_name: str = "",
    umbrella_id: str = "",
    anteilsklasse: str = "",
    ausschuettungsart: str = "",
    fondswaehrung: str = "",
    fundinfo_ter: str = "",
    prospekt_url: str = "",
):
    if not isin:
        return
    _ensure_columns()
    with _connect() as con:
        con.execute("""
            UPDATE fund_results SET
                subfonds_id       = CASE WHEN subfonds_id       = '' OR subfonds_id       IS NULL THEN ? ELSE subfonds_id       END,
                subfonds_name     = CASE WHEN subfonds_name     = '' OR subfonds_name     IS NULL THEN ? ELSE subfonds_name     END,
                umbrella_id       = CASE WHEN umbrella_id       = '' OR umbrella_id       IS NULL THEN ? ELSE umbrella_id       END,
                anteilsklasse     = CASE WHEN anteilsklasse     = '' OR anteilsklasse     IS NULL THEN ? ELSE anteilsklasse     END,
                ausschuettungsart = CASE WHEN ausschuettungsart = '' OR ausschuettungsart IS NULL THEN ? ELSE ausschuettungsart END,
                fondswaehrung     = CASE WHEN fondswaehrung     = '' OR fondswaehrung     IS NULL THEN ? ELSE fondswaehrung     END,
                fundinfo_ter      = CASE WHEN fundinfo_ter      = '' OR fundinfo_ter      IS NULL THEN ? ELSE fundinfo_ter      END,
                prospekt_url      = CASE WHEN prospekt_url      = '' OR prospekt_url      IS NULL THEN ? ELSE prospekt_url      END
            WHERE isin = ?
        """, (subfonds_id, subfonds_name, umbrella_id, anteilsklasse,
              ausschuettungsart, fondswaehrung, fundinfo_ter, prospekt_url, isin))


def get_subfonds_groups() -> dict:
    rows = get_all_results()
    groups: dict = {}
    for row in rows:
        key = row.get("subfonds_id") or ""
        groups.setdefault(key, []).append(row)
    return groups


def get_by_prospekt_url(url: str) -> Optional[dict]:
    """Gibt den ersten DB-Eintrag zurück, der diese Prospekt-URL bereits hat."""
    if not url:
        return None
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM fund_results WHERE prospekt_url = ? AND prospekt_pfad != '' LIMIT 1",
            (url,)
        ).fetchone()
    return dict(row) if row else None


def update_prospekt(isin: str, prospekt_pfad: str, prospekt_url: str):
    with _connect() as con:
        con.execute(
            "UPDATE fund_results SET prospekt_pfad=?, prospekt_url=? WHERE isin=?",
            (prospekt_pfad or "", prospekt_url or "", isin),
        )


def get_prospekt_queue() -> list[dict]:
    rows = get_all_results()
    return [
        r for r in rows
        if not r.get("prospekt_pfad") or not Path(r["prospekt_pfad"]).exists()
    ]
