"""
Typologie-Verwaltung — kanonisches Werte-Universum für Fondstyp, Anlegertyp, Kundentyp.

Gespeichert als eigene Tabelle `typologie` in derselben SQLite-DB wie die Ergebnisdatenbank.
"""

import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent / "data" / "output" / "results.db"

# Initiale Seed-Daten: (feld, wert, segment, synonyme, sortierung)
_SEED: list[tuple] = [
    # ── Fondstyp ─────────────────────────────────────────────────────────────
    ("fondstyp", "ETF",                   "retail",        "Exchange Traded Fund, exchange traded, passiv börsengehandelt", 1),
    ("fondstyp", "Anlagestiftung",        "institutional", "investment foundation, Stiftungsfonds", 2),
    ("fondstyp", "Publikumsfonds",        "retail",        "public fund, mutual fund, UCITS retail, fonds public, OGAW", 3),
    ("fondstyp", "Institutioneller Fonds","institutional", "institutional fund, AIF, alternative investment fund, Spezialfonds", 4),

    # ── Anlegertyp ───────────────────────────────────────────────────────────
    ("anlegertyp", "Professionelle Anleger", "institutional",
     "professional investors, professional clients, MiFID professional, professionelle Kunden", 1),
    ("anlegertyp", "Privat",                 "retail",
     "retail, private investors, Privatanleger, retail clients, all investors", 2),
    ("anlegertyp", "Qualifizierte Anleger",  "institutional",
     "qualified investors, qualified purchasers, KAG Art. 10, CISA qualified", 3),

    # ── Kundentyp ────────────────────────────────────────────────────────────
    ("kundentyp", "Pensionskassen",                                              "institutional", "pension funds, Vorsorgeeinrichtungen", 1),
    ("kundentyp", "Pensionsfonds",                                               "institutional", "pension fund, Pensionskasse", 2),
    ("kundentyp", "Einrichtungen der beruflichen Vorsorge",                      "institutional", "occupational pension, BVG, berufliche Vorsorge, 2nd pillar", 3),
    ("kundentyp", "Versicherungen",                                              "institutional", "insurance companies, insurers, Versicherungsunternehmen", 4),
    ("kundentyp", "Stiftungen",                                                  "institutional", "foundations, charitable foundations, Stiftung", 5),
    ("kundentyp", "Institutionen",                                               "institutional", "institutions, institutional investors, institutionelle Anleger", 6),
    ("kundentyp", "Öffentlich-rechtliche Körperschaften mit professioneller Tresorie",
                                                                                 "institutional", "public law entities, government treasury, Körperschaften öffentlichen Rechts", 7),
    ("kundentyp", "Grossanleger",                                                "institutional", "large investors, wholesale investors", 8),
    ("kundentyp", "Captive Channel",                                             "institutional", "captive distribution, group internal channel", 9),
    ("kundentyp", "Family Offices",                                              "institutional", "family office, wealth management family", 10),
    ("kundentyp", "Finanzintermediäre",                                          "institutional", "financial intermediaries, distributors, Vertriebspartner, banks distributing", 11),
    ("kundentyp", "Anleger mit individuellen Gebührenvereinbarungen",            "institutional", "bespoke fee, individual fee agreement, fee-based", 12),
    ("kundentyp", "Kunden mit professionellem Vermögensverwaltungsvertrag",      "institutional", "discretionary mandate, DPM, portfolio management agreement", 13),
    ("kundentyp", "Kunden mit unabhängigem Beratungsvertrag mit einem Finanzintermediär",
                                                                                 "institutional", "independent advisor, IFA, advisory mandate, unabhängige Beratung", 14),
    ("kundentyp", "Anteilsklassen ohne Gebühren (no-Load)",                     "institutional", "no-load, clean share class, no retrocession, no trailer fee, clean class", 15),
    ("kundentyp", "Sehr vermögende PrivatKunden",                                "institutional", "high net worth, wealthy clients, affluent private", 16),
    ("kundentyp", "HNWI",                                                        "institutional", "high net worth individual, HNW, vermögende Privatpersonen", 17),
    ("kundentyp", "UHNWI",                                                       "institutional", "ultra high net worth individual, UHNW, ultra wealthy", 18),
    ("kundentyp", "Privatanleger",                                               "retail",        "retail investors, private clients, Retail, Privatkunden", 19),
    ("kundentyp", "Mindestanlage (retail)",                                      "retail",        "low minimum investment, retail minimum, kleines Mindestinvestment", 20),
    ("kundentyp", "Mindestanlage (institutionell)",                              "institutional", "high minimum investment, large minimum, hohes Mindestinvestment", 21),
]


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_typologie_db():
    """Erstellt die Tabelle und befüllt sie einmalig mit den Seed-Daten."""
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS typologie (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                feld       TEXT NOT NULL,
                wert       TEXT NOT NULL,
                segment    TEXT DEFAULT '',
                synonyme   TEXT DEFAULT '',
                sortierung INTEGER DEFAULT 0,
                UNIQUE(feld, wert)
            )
        """)
        # Seed nur einfügen wenn Tabelle leer
        count = con.execute("SELECT COUNT(*) FROM typologie").fetchone()[0]
        if count == 0:
            con.executemany(
                "INSERT OR IGNORE INTO typologie (feld, wert, segment, synonyme, sortierung) VALUES (?,?,?,?,?)",
                _SEED,
            )


def get_alle_werte() -> list[dict]:
    """Gibt alle Einträge sortiert nach feld + sortierung zurück."""
    init_typologie_db()
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM typologie ORDER BY feld, sortierung, wert"
        ).fetchall()
    return [dict(r) for r in rows]


def get_werte(feld: str) -> list[dict]:
    """Gibt alle Werte eines Felds zurück."""
    init_typologie_db()
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM typologie WHERE feld=? ORDER BY sortierung, wert", (feld,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_wert_liste(feld: str) -> list[str]:
    """Gibt nur die Wert-Strings eines Felds zurück (für Prompt-Injektion)."""
    return [r["wert"] for r in get_werte(feld)]


def add_wert(feld: str, wert: str, segment: str = "", synonyme: str = "") -> bool:
    """Fügt einen neuen Wert hinzu. Returns False wenn schon vorhanden."""
    init_typologie_db()
    try:
        with _connect() as con:
            max_sort = con.execute(
                "SELECT MAX(sortierung) FROM typologie WHERE feld=?", (feld,)
            ).fetchone()[0] or 0
            con.execute(
                "INSERT INTO typologie (feld, wert, segment, synonyme, sortierung) VALUES (?,?,?,?,?)",
                (feld, wert.strip(), segment, synonyme, max_sort + 1),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def update_wert(id_: int, wert: str, segment: str, synonyme: str):
    """Aktualisiert einen Eintrag anhand der ID."""
    with _connect() as con:
        con.execute(
            "UPDATE typologie SET wert=?, segment=?, synonyme=? WHERE id=?",
            (wert.strip(), segment, synonyme, id_),
        )


def delete_wert(id_: int):
    """Löscht einen Eintrag."""
    with _connect() as con:
        con.execute("DELETE FROM typologie WHERE id=?", (id_,))


# Beim Import initialisieren
init_typologie_db()
