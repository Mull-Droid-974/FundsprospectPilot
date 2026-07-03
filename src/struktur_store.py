"""
Fondsstruktur — hierarchische, Morningstar-angereicherte und geprüfte Sicht.

Baut aus fund_results + morningstar_data die Struktur
    Umbrella (Branding/Fund Family) → Portfolio (Subfonds) → Anteilsklasse
mit sauberen Identifiern/Bezeichnungen und einer Konsistenz-Prüfung.

Datenrealität:
- Portfolio-Ebene = subfonds_id (echt gruppierend). umbrella_id ist quasi eindeutig
  und daher NICHT als Umbrella nutzbar → Umbrella kommt aus Morningstar `ms_umbrella`
  (Branding Name, via Enrichment nachgeladen).
- "Hat Morningstar-Daten" wird an ms_category/ms_status festgemacht (immer befüllt),
  nicht an ms_secid (wird nachgeladen).
"""

import re
import sqlite3
from collections import Counter
from pathlib import Path

_DB = Path(__file__).parent.parent / "data" / "output" / "results.db"

OK, WARN, ERROR = "ok", "warn", "error"
_RANK = {OK: 0, WARN: 1, ERROR: 2}

# morningstar_data-Spalte → interner Schlüssel
_MS_FIELDS = {
    "ms_secid": "secid", "ms_name": "ms_name", "ms_umbrella": "umbrella",
    "ms_category": "kategorie", "ms_share_class_status": "status",
    "ms_domicile": "domizil", "ms_currency": "ms_currency",
    "ms_ongoing_charge": "ter", "ms_rating": "rating",
}


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_DB, timeout=15)  # wartet, falls parallel geschrieben wird
    con.row_factory = sqlite3.Row
    return con


def _norm(s: str) -> set:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9äöü ]+", " ", s)
    stop = {"fund", "funds", "fonds", "sicav", "ucits", "plc", "the", "of", "and",
            "sub", "class", "klasse", "anteilsklasse", "lux", "irl"}
    return {t for t in s.split() if len(t) > 2 and t not in stop}


def _name_mismatch(a: str, b: str) -> bool:
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return False
    return len(ta & tb) == 0


def _worst(statuses) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 0)) if statuses else OK


def _check_klasse(row: dict, ms: dict, has_ms: bool) -> tuple[str, list[str]]:
    """Prüft Identifier/Bezeichnungen einer Anteilsklasse. → (status, issues)."""
    issues, status = [], OK
    if not has_ms:
        issues.append("keine Morningstar-Daten (ISIN nicht gefunden)")
        status = ERROR
    if not (row.get("subfonds_id") or "").strip():
        issues.append("subfonds_id fehlt"); status = _worst([status, WARN])
    if not (row.get("anteilsklasse") or "").strip():
        issues.append("Anteilsklassen-Bezeichnung leer"); status = _worst([status, WARN])
    ms_name = ms.get("ms_name") or ""
    local = row.get("subfonds_name") or row.get("fondsname") or ""
    if local and ms_name and _name_mismatch(local, ms_name):
        issues.append(f"Name weicht ab: „{local}“ ↔ MS „{ms_name}“")
        status = _worst([status, WARN])
    if (ms.get("status") or "").lower() not in ("", "active"):
        issues.append(f"MS-Status: {ms.get('status')}"); status = _worst([status, WARN])
    return status, issues


def _most_common(vals: list[str]) -> str:
    vals = [v for v in vals if v]
    return Counter(vals).most_common(1)[0][0] if vals else ""


def build_structure(isins: list[str] | None = None) -> list[dict]:
    """Baut die Hierarchie Umbrella → Subfonds → Anteilsklasse (angereichert + geprüft)."""
    con = _connect()
    q = "SELECT * FROM fund_results"
    params = ()
    if isins:
        ph = ",".join("?" * len(isins))
        q += f" WHERE isin IN ({ph})"; params = tuple(isins)
    rows = [dict(r) for r in con.execute(q, params).fetchall()]
    ms_by_isin = {}
    for r in con.execute("SELECT * FROM morningstar_data").fetchall():
        d = dict(r)
        ms_by_isin[d["isin"]] = {v: (d.get(k) or "") for k, v in _MS_FIELDS.items()}
    con.close()

    # 1) Subfonds gruppieren (echtes Portfolio-Level)
    subs: dict = {}
    for r in rows:
        sid = (r.get("subfonds_id") or "").strip() or f"__single_{r['isin']}"
        subs.setdefault(sid, []).append(r)

    # 2) Subfonds-Knoten + Umbrella (Branding) je Subfonds bestimmen
    sub_nodes = []
    for sid, grp in subs.items():
        kl_nodes, brandings = [], []
        for r in grp:
            ms = ms_by_isin.get(r["isin"], {})
            has_ms = bool(ms.get("kategorie") or ms.get("status") or ms.get("secid"))
            st, issues = _check_klasse(r, ms, has_ms)
            if ms.get("umbrella"):
                brandings.append(ms["umbrella"])
            kl_nodes.append({
                "isin": r["isin"], "fund_id": r.get("fund_id") or "",
                "secid": ms.get("secid") or "",
                "anteilsklasse": r.get("anteilsklasse") or "",
                "ms_name": ms.get("ms_name") or "",
                "kategorie": ms.get("kategorie") or "", "status": ms.get("status") or "",
                "domizil": ms.get("domizil") or "",
                "waehrung": r.get("fondswaehrung") or ms.get("ms_currency") or "",
                "ter": ms.get("ter") or r.get("fundinfo_ter") or "",
                "rating": ms.get("rating") or "",
                "fondstyp": r.get("fondstyp") or "", "anlegertyp": r.get("anlegertyp") or "",
                "kundentyp": r.get("kundentyp") or "",
                "check": st, "issues": issues,
            })
        sub_nodes.append({
            "subfonds_id": grp[0].get("subfonds_id") or "",
            "subfonds_name": grp[0].get("subfonds_name") or "(ohne Namen)",
            "umbrella": _most_common(brandings),
            "n_klassen": len(kl_nodes),
            "check": _worst([k["check"] for k in kl_nodes]),
            "anteilsklassen": sorted(kl_nodes, key=lambda k: k["anteilsklasse"]),
        })

    # 3) Subfonds nach Umbrella (Branding) gruppieren
    umb: dict = {}
    for s in sub_nodes:
        key = s["umbrella"] or "(ohne Umbrella-Zuordnung)"
        umb.setdefault(key, []).append(s)

    result = []
    for uname, sn in umb.items():
        result.append({
            "umbrella_name": uname,
            "n_subfonds": len(sn),
            "n_klassen": sum(s["n_klassen"] for s in sn),
            "check": _worst([s["check"] for s in sn]),
            "subfonds": sorted(sn, key=lambda s: s["subfonds_name"]),
        })
    # Auffällige (error/warn) zuerst, dann alphabetisch
    result.sort(key=lambda u: (-_RANK[u["check"]], u["umbrella_name"]))
    return result


def structure_stats(tree: list[dict]) -> dict:
    kl_status = {OK: 0, WARN: 0, ERROR: 0}
    for u in tree:
        for s in u["subfonds"]:
            for k in s["anteilsklassen"]:
                kl_status[k["check"]] += 1
    return {"umbrellas": len(tree),
            "subfonds": sum(u["n_subfonds"] for u in tree),
            "klassen": sum(u["n_klassen"] for u in tree),
            "klassen_status": kl_status}
