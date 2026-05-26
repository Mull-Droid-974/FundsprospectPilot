"""
Morningstar Datenspeicher — separate SQLite-Tabelle `morningstar_data`.

Gleiche DB-Datei wie results_store, eigene Tabelle.
Kein GUI-Code — reine Datenbankschicht.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent / "data" / "output" / "results.db"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def ensure_morningstar_table():
    """Erstellt die morningstar_data Tabelle falls nötig. Sicher mehrfach aufrufbar."""
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS morningstar_data (
                isin                  TEXT PRIMARY KEY,
                ms_secid              TEXT DEFAULT '',
                ms_name               TEXT DEFAULT '',
                ms_category           TEXT DEFAULT '',
                ms_category_id        TEXT DEFAULT '',
                ms_asset_class        TEXT DEFAULT '',
                ms_legal_type         TEXT DEFAULT '',
                ms_umbrella           TEXT DEFAULT '',
                ms_share_class_type   TEXT DEFAULT '',
                ms_share_class_status TEXT DEFAULT '',
                ms_inception_date     TEXT DEFAULT '',
                ms_termination_date   TEXT DEFAULT '',
                ms_domicile           TEXT DEFAULT '',
                ms_currency           TEXT DEFAULT '',
                ms_ongoing_charge     TEXT DEFAULT '',
                ms_min_investment     TEXT DEFAULT '',
                ms_investor_type      TEXT DEFAULT '',
                ms_mifid_category     TEXT DEFAULT '',
                ms_rating             TEXT DEFAULT '',
                ms_risk_rating        TEXT DEFAULT '',
                ms_fetch_date         TEXT DEFAULT '',
                ms_raw_json           TEXT DEFAULT ''
            )
        """)
        # Migration: neue Spalten zu bestehenden Tabellen hinzufügen
        for col_def in [
            "ms_secid TEXT DEFAULT ''",
            "ms_name TEXT DEFAULT ''",
            "ms_category TEXT DEFAULT ''",
            "ms_category_id TEXT DEFAULT ''",
            "ms_asset_class TEXT DEFAULT ''",
            "ms_legal_type TEXT DEFAULT ''",
            "ms_umbrella TEXT DEFAULT ''",
            "ms_share_class_type TEXT DEFAULT ''",
            "ms_share_class_status TEXT DEFAULT ''",
            "ms_inception_date TEXT DEFAULT ''",
            "ms_termination_date TEXT DEFAULT ''",
            "ms_domicile TEXT DEFAULT ''",
            "ms_currency TEXT DEFAULT ''",
            "ms_ongoing_charge TEXT DEFAULT ''",
            "ms_min_investment TEXT DEFAULT ''",
            "ms_investor_type TEXT DEFAULT ''",
            "ms_mifid_category TEXT DEFAULT ''",
            "ms_rating TEXT DEFAULT ''",
            "ms_risk_rating TEXT DEFAULT ''",
            "ms_fetch_date TEXT DEFAULT ''",
            "ms_raw_json TEXT DEFAULT ''",
        ]:
            try:
                con.execute(f"ALTER TABLE morningstar_data ADD COLUMN {col_def}")
            except Exception:
                pass


def upsert_morningstar(isin: str, data: dict):
    """Speichert oder aktualisiert Morningstar-Daten für eine ISIN."""
    if not isin:
        return
    ensure_morningstar_table()

    fields = [
        "ms_secid", "ms_name", "ms_category", "ms_category_id",
        "ms_asset_class", "ms_legal_type", "ms_umbrella",
        "ms_share_class_type", "ms_share_class_status",
        "ms_inception_date", "ms_termination_date",
        "ms_domicile", "ms_currency", "ms_ongoing_charge",
        "ms_min_investment", "ms_investor_type", "ms_mifid_category",
        "ms_rating", "ms_risk_rating", "ms_raw_json",
    ]

    row = {f: str(data.get(f, "") or "") for f in fields}
    row["isin"] = isin
    row["ms_fetch_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    with _connect() as con:
        con.execute(
            f"INSERT OR REPLACE INTO morningstar_data ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )


def get_morningstar(isin: str) -> Optional[dict]:
    """Gibt Morningstar-Daten für eine ISIN zurück, oder None."""
    ensure_morningstar_table()
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM morningstar_data WHERE isin = ?", (isin,)
        ).fetchone()
    return dict(row) if row else None


def get_all_morningstar() -> list[dict]:
    """Gibt alle Morningstar-Datensätze zurück."""
    ensure_morningstar_table()
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM morningstar_data ORDER BY ms_fetch_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_isins_without_morningstar() -> list[str]:
    """Gibt alle ISINs aus fund_results zurück, für die noch kein MS-Datensatz existiert."""
    ensure_morningstar_table()
    with _connect() as con:
        rows = con.execute("""
            SELECT fr.isin
            FROM fund_results fr
            LEFT JOIN morningstar_data md ON fr.isin = md.isin
            WHERE md.isin IS NULL
            ORDER BY fr.isin
        """).fetchall()
    return [r["isin"] for r in rows]


def get_all_isins() -> list[str]:
    """Gibt alle ISINs aus fund_results zurück."""
    with _connect() as con:
        rows = con.execute("SELECT isin FROM fund_results ORDER BY isin").fetchall()
    return [r["isin"] for r in rows]


def delete_morningstar(isin: str):
    """Löscht Morningstar-Daten für eine ISIN."""
    ensure_morningstar_table()
    with _connect() as con:
        con.execute("DELETE FROM morningstar_data WHERE isin = ?", (isin,))


def get_stats() -> dict:
    """Gibt Zählstatistik zurück."""
    ensure_morningstar_table()
    with _connect() as con:
        total_fund = con.execute("SELECT COUNT(*) FROM fund_results").fetchone()[0]
        total_ms = con.execute("SELECT COUNT(*) FROM morningstar_data").fetchone()[0]
    return {"total_fund": total_fund, "total_morningstar": total_ms,
            "missing": total_fund - total_ms}
