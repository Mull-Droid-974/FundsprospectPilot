"""
fundinfo.com Prospekt-Download via JSON API.

Endpoint (verifiziiert):
  GET https://www.fundinfo.com/en/{profile}/LandingPage/Data
      ?skip=0&query={ISIN}&orderdirection=desc

Response-Struktur:
  Data[0].D["PR"]  →  Liste von Prospekt-Dokumenten
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from utils import logger, build_pdf_filename, get_next_pdf_number

# Profile-Reihenfolge bei Fallback
PROFILES = ["CH-prof", "CH-pub", "DE-prof", "LU-prof", "AT-prof", "FR-prof", "IT-prof", "NL-prof", "GB-prof"]

# Sprach-Präferenz für Prospekte
LANG_PREFERENCE = ["DE", "EN", "FR", "IT", "ES"]

# Browser-ähnliche Headers (verhindert 403-Fehler)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.fundinfo.com/",
}

# Cookies für fundinfo.com
COOKIES = {
    "DU": "CH-prof",
    "PrivacyPolicy": "1",
}

REQUEST_TIMEOUT = 30


@dataclass
class DownloadResult:
    pdf_path: str
    pdf_url: str
    language: str
    profile: str


def _get_session() -> requests.Session:
    """Erstellt eine Session mit Browser-Headers und Cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)
    return session


def _query_api(isin: str, profile: str, session: requests.Session) -> Optional[dict]:
    """
    Ruft die fundinfo.com JSON-API auf und gibt das rohe D-Dict zurück.
    D enthält alle verfügbaren Dokumenttypen als Keys (z.B. "PR", "KI", "WAI", ...).
    """
    item = _query_api_full(isin, profile, session)
    if item is None:
        return None
    return item.get("D", {})


def _query_api_full(isin: str, profile: str, session: requests.Session) -> Optional[dict]:
    """Wie _query_api, gibt aber das vollständige Data[0]-Objekt zurück (inkl. S, D, R)."""
    api_url = f"https://www.fundinfo.com/en/{profile}/LandingPage/Data"
    params = {"skip": 0, "query": isin, "orderdirection": "desc"}
    try:
        resp = session.get(api_url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("Data", [])
        if not items:
            return None
        return items[0]
    except requests.RequestException as e:
        logger.debug(f"fundinfo API Fehler (Profil {profile}): {e}")
        return None
    except (KeyError, IndexError, ValueError) as e:
        logger.debug(f"fundinfo Antwort Parse-Fehler (Profil {profile}): {e}")
        return None


def _best_doc_from_list(docs: list) -> Optional[dict]:
    """Wählt das beste Dokument (aktiv, bevorzugte Sprache, neuestes Datum)."""
    if not docs:
        return None
    active = [d for d in docs if d.get("Active", True)] or docs

    def sort_key(doc):
        lang = doc.get("Language", "XX")
        lang_rank = LANG_PREFERENCE.index(lang) if lang in LANG_PREFERENCE else 99
        return (lang_rank, doc.get("Date", "1900-01-01"))

    best = sorted(active, key=sort_key)[0]
    return {
        "url":      best.get("Url", ""),
        "language": best.get("Language", ""),
        "date":     best.get("Date", ""),
    }


def _discover_pdf_url(isin: str, profile: str, session: requests.Session) -> Optional[dict]:
    """
    Ruft die fundinfo.com JSON-API auf und findet den Verkaufsprospekt.

    Returns:
        {"url": "...", "language": "DE", "date": "2024-01-01"} oder None
    """
    d = _query_api(isin, profile, session)
    if d is None:
        return None
    return _best_doc_from_list(d.get("PR", []))


def _download_pdf(
    url: str,
    pdf_folder: str,
    fund_name: str,
    session: requests.Session,
) -> Optional[str]:
    """
    Lädt eine PDF von der URL herunter und validiert sie.

    Returns:
        Lokaler Dateipfad oder None bei Fehler.
    """
    folder = Path(pdf_folder)
    folder.mkdir(parents=True, exist_ok=True)

    number = get_next_pdf_number(pdf_folder)
    filename = build_pdf_filename(number, fund_name)
    save_path = folder / filename

    try:
        logger.info(f"Lade PDF: {url[:80]}")
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()

        # Inhalt in Chunks lesen
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)

        content = b"".join(chunks)

        # Validierung: Ist es wirklich eine PDF?
        if not content.startswith(b"%PDF"):
            logger.warning(f"Heruntergeladene Datei ist keine PDF (Magic Bytes fehlen): {url}")
            # Trotzdem speichern – manche PDFs haben kleine Header-Offsets
            if b"%PDF" not in content[:1024]:
                return None

        # Grössencheck (max 50 MB)
        size_mb = len(content) / (1024 * 1024)
        if size_mb > 50:
            logger.warning(f"PDF zu gross ({size_mb:.1f} MB), überspringe: {filename}")
            return None

        with open(save_path, "wb") as f:
            f.write(content)

        logger.info(f"PDF gespeichert: {filename} ({size_mb:.1f} MB)")
        return str(save_path)

    except requests.RequestException as e:
        logger.error(f"Download-Fehler: {e}")
        if save_path.exists():
            save_path.unlink()
        return None


def _is_valid_isin(val: str) -> bool:
    v = val.strip()
    return len(v) == 12 and v[:2].isalpha() and v[2:].isalnum()


def _extract_isin_from_item(item: dict) -> str:
    """Extrahiert die ISIN aus einem fundinfo Data-Item (R- oder S-Dict)."""
    r = item.get("R") or {}
    s = item.get("S") or {}
    # Bekannte Feldnamen im R-Dict
    for key in ("ISIN", "Isin", "isin", "Isin_Code", "IsinCode"):
        val = str(r.get(key) or "").strip()
        if _is_valid_isin(val):
            return val.upper()
    # Bekannte Feldnamen im S-Dict
    for field in ("OFST000001", "OFST010001", "OFST000100"):
        val = str(s.get(field) or "").strip()
        if _is_valid_isin(val):
            return val.upper()
    # Fallback: alle S-Werte die ISIN-Format erfüllen
    for val in s.values():
        if isinstance(val, str) and _is_valid_isin(val):
            return val.strip().upper()
    return ""


def fetch_subfonds_isins(
    subfonds_name: str,
    umbrella_id: str,
    profile: str,
    delay: float = 0.5,
) -> list[dict]:
    """
    Holt alle Anteilsklassen (ISINs) eines Subfonds von fundinfo.

    Abfrage per subfonds_name → filtert nach umbrella_id und exaktem subfonds_name.
    Gibt eine Liste von Metadaten-Dicts zurück (gleiche Struktur wie fetch_fund_metadata).
    """
    if not subfonds_name or subfonds_name == "—":
        return []
    session = _get_session()
    time.sleep(delay)
    api_url = f"https://www.fundinfo.com/en/{profile}/LandingPage/Data"
    params = {"skip": 0, "take": 200, "query": subfonds_name, "orderdirection": "desc"}
    try:
        resp = session.get(api_url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("Data", [])
    except Exception as exc:
        logger.debug(f"fetch_subfonds_isins Fehler ({profile}): {exc}")
        return []

    results = []
    for item in items:
        s = item.get("S") or {}
        item_umbrella = (s.get("OFST900000") or "").strip()
        item_sf_name  = (s.get("OFST900016") or "").strip()

        # Nur ISINs desselben Umbrella und Subfonds
        if umbrella_id and umbrella_id != "—" and item_umbrella != umbrella_id:
            continue
        if item_sf_name != subfonds_name:
            continue

        isin = _extract_isin_from_item(item)
        if not isin:
            continue

        doc_info = _best_doc_from_list(item.get("D", {}).get("PR", []))
        results.append({
            "isin":                   isin,
            "subfonds_id":            s.get("OFST900017", ""),
            "subfonds_name":          item_sf_name,
            "umbrella_id":            item_umbrella,
            "anteilsklasse":          s.get("OFST020050", ""),
            "ausschuettungsart":      s.get("OFST020400", ""),
            "fondswaehrung":          s.get("OFST010410", ""),
            "fundinfo_ter":           s.get("OFST452000", ""),
            "subfonds_code":          s.get("OFST900171", ""),
            "fundinfo_investor_type": s.get("OFST900267", ""),
            "ongoing_charges_datum":  s.get("OFST452110", ""),
            "qualif_anleger_ch":      s.get("OFST6030CH", ""),
            "institutional_ch":       s.get("OFST6031CH", ""),
            "prospekt_url":           doc_info["url"] if doc_info else "",
            "prospekt_lang":          doc_info.get("language", "") if doc_info else "",
            "profile":                profile,
        })
    return results


def fetch_fund_metadata(isin: str, delay: float = 1.0) -> Optional[dict]:
    """
    Ruft alle Stammdaten (S-Dict) + Prospekt-URL für eine ISIN ab.
    Einmaliger API-Call pro ISIN — kein Download.

    Returns dict mit Schlüsseln:
        subfonds_id, subfonds_name, umbrella_id, anteilsklasse,
        ausschuettungsart, fondswaehrung, fundinfo_ter,
        prospekt_url, prospekt_lang, subfonds_code, profile
    """
    session = _get_session()
    for profile in PROFILES:
        time.sleep(delay)
        item = _query_api_full(isin, profile, session)
        if item is None:
            continue
        s = item.get("S") or {}
        doc_info = _best_doc_from_list(item.get("D", {}).get("PR", []))
        return {
            "subfonds_id":       s.get("OFST900017", ""),
            "subfonds_name":     s.get("OFST900016", ""),
            "umbrella_id":       s.get("OFST900000", ""),
            "anteilsklasse":     s.get("OFST020050", ""),
            "ausschuettungsart": s.get("OFST020400", ""),
            "fondswaehrung":     s.get("OFST010410", ""),
            "fundinfo_ter":      s.get("OFST452000", ""),
            "subfonds_code":          s.get("OFST900171", ""),
            "fundinfo_investor_type": s.get("OFST900267", ""),
            "ongoing_charges_datum":  s.get("OFST452110", ""),
            "qualif_anleger_ch":      s.get("OFST6030CH", ""),
            "institutional_ch":       s.get("OFST6031CH", ""),
            "prospekt_url":      doc_info["url"] if doc_info else "",
            "prospekt_lang":     doc_info.get("language", "") if doc_info else "",
            "profile":           profile,
        }
    return None


def download_prospekt_from_url(
    url: str,
    subfonds_code: str,
    language: str,
    pdf_folder: str,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """
    Lädt einen Prospekt von einer bekannten URL herunter.
    Dateiname: {subfonds_code}_{language}.pdf  (z.B. FAFJA_EN.pdf)
    Fallback:  {subfonds_code[:8]}_{language}.pdf wenn Code leer.
    Idempotent: existiert die Datei bereits, wird sie direkt zurückgegeben.
    """
    from utils import sanitize_filename
    folder = Path(pdf_folder)
    folder.mkdir(parents=True, exist_ok=True)

    code = sanitize_filename(subfonds_code) if subfonds_code else ""
    lang = language.upper() if language else "XX"
    filename = f"{code}_{lang}.pdf" if code else f"prospekt_{lang}.pdf"
    save_path = folder / filename

    if save_path.exists():
        logger.info(f"Prospekt bereits vorhanden: {filename}")
        return str(save_path)

    if session is None:
        session = _get_session()

    try:
        logger.info(f"Lade Prospekt: {url[:80]}")
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
        content = b"".join(chunks)

        if not content.startswith(b"%PDF") and b"%PDF" not in content[:1024]:
            logger.warning(f"Heruntergeladene Datei ist keine PDF: {url[:80]}")
            return None

        size_mb = len(content) / (1024 * 1024)
        if size_mb > 50:
            logger.warning(f"PDF zu gross ({size_mb:.1f} MB): {filename}")
            return None

        with open(save_path, "wb") as f:
            f.write(content)
        logger.info(f"Prospekt gespeichert: {filename} ({size_mb:.1f} MB)")
        return str(save_path)

    except requests.RequestException as e:
        logger.error(f"Download-Fehler: {e}")
        if save_path.exists():
            save_path.unlink()
        return None


def discover_prospectus_url(
    isin: str,
    delay: float = 1.5,
) -> Optional[dict]:
    """
    Ermittelt die Prospekt-URL für eine ISIN ohne Download.

    Returns:
        {"url": str, "language": str, "profile": str} oder None.
    """
    session = _get_session()
    for profile in PROFILES:
        time.sleep(delay)
        doc_info = _discover_pdf_url(isin, profile, session)
        if doc_info and doc_info.get("url"):
            return {
                "url":      doc_info["url"],
                "language": doc_info.get("language", ""),
                "profile":  profile,
            }
    return None


def fetch_prospectus(
    isin: str,
    fund_name: str,
    pdf_folder: str,
    delay: float = 1.5,
) -> Optional[DownloadResult]:
    """
    Kompletter Workflow: Suche + Download für eine ISIN.

    Probiert mehrere fundinfo.com Profile (CH-prof → CH-pub → DE-prof → LU-prof).

    Returns:
        DownloadResult mit lokalem Pfad, oder None falls nicht gefunden.
    """
    session = _get_session()

    for profile in PROFILES:
        # Rate Limiting
        time.sleep(delay)

        logger.info(f"Suche Prospekt für {isin} (Profil: {profile})")
        doc_info = _discover_pdf_url(isin, profile, session)

        if not doc_info or not doc_info.get("url"):
            logger.debug(f"Kein Prospekt gefunden in Profil {profile}")
            continue

        pdf_url = doc_info["url"]
        time.sleep(0.5)

        pdf_path = _download_pdf(pdf_url, pdf_folder, fund_name, session)
        if pdf_path:
            return DownloadResult(
                pdf_path=pdf_path,
                pdf_url=pdf_url,
                language=doc_info.get("language", ""),
                profile=profile,
            )

    logger.warning(f"Kein Prospekt auf fundinfo.com für ISIN: {isin}")
    return None


# ─── Comparison-Dokumente ─────────────────────────────────────────────────────

# Mögliche fundinfo D-Keys für Factsheets / Jahresberichte / Halbjahresberichte.
# Die genauen Keys variieren je nach Fonds/Profil; wir probieren alle durch.
_FACTSHEET_KEYS    = ["FS", "FS2", "SH", "KF", "FSH", "FCS", "FACT"]
_ANNUAL_RPT_KEYS   = ["AR", "JB", "GB", "ANN", "ANR", "YR", "RAJ", "ARA"]
_HALFYEAR_RPT_KEYS = ["SAR", "SR", "HJB", "HB", "HAR", "SYR", "SRA", "RASM"]


def _download_named(
    url: str,
    folder: Path,
    filename: str,
    session: requests.Session,
    max_mb: float = 30.0,
) -> Optional[str]:
    """Lädt ein Dokument mit festem Dateinamen herunter. Cacht vorhandene Dateien."""
    save_path = folder / filename
    if save_path.exists():
        logger.info(f"Dokument gecacht: {filename}")
        return str(save_path)
    try:
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content.startswith(b"%PDF") and b"%PDF" not in content[:1024]:
            logger.warning(f"Dokument ist keine PDF: {url[:80]}")
            return None
        size_mb = len(content) / (1024 * 1024)
        if size_mb > max_mb:
            logger.warning(f"Dokument zu gross ({size_mb:.1f} MB): {filename}")
            return None
        with open(save_path, "wb") as f:
            f.write(content)
        logger.info(f"Gespeichert: {filename} ({size_mb:.2f} MB)")
        return str(save_path)
    except requests.RequestException as e:
        logger.error(f"Download-Fehler für {filename}: {e}")
        if save_path.exists():
            save_path.unlink()
        return None


def fetch_comparison_docs(
    isin: str,
    save_folder: str,
    delay: float = 1.5,
) -> dict:
    """
    Ruft Metadaten und Vergleichsdokumente (Factsheet, Jahresbericht,
    Halbjahresbericht) für eine ISIN von fundinfo.com ab.

    Returns:
        {
          "factsheet":    str | None,   # lokaler Pfad
          "annual":       str | None,
          "halfyear":     str | None,
          "available_keys": list[str],  # alle D-Keys mit Inhalt (für Debugging)
          "meta": {
              "name":          str,
              "anteilsklasse": str,
              "waehrung":      str,
              "ter":           str,
              "profile":       str,
          }
        }
    """
    folder = Path(save_folder)
    folder.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "factsheet":      None,
        "annual":         None,
        "halfyear":       None,
        "available_keys": [],
        "meta":           {},
    }

    session = _get_session()

    for profile in PROFILES:
        time.sleep(delay)
        item = _query_api_full(isin, profile, session)
        if item is None:
            continue

        s = item.get("S") or {}
        d = item.get("D") or {}

        result["meta"] = {
            "name":          s.get("OFST900016", ""),
            "anteilsklasse": s.get("OFST020050", ""),
            "waehrung":      s.get("OFST010410", ""),
            "ter":           s.get("OFST452000", ""),
            "profile":       profile,
        }

        available = [k for k, v in d.items() if v]
        result["available_keys"] = available
        logger.info(f"fundinfo D-Keys für {isin} @ {profile}: {available}")

        def _try_keys(keys: list[str], prefix: str) -> Optional[str]:
            for key in keys:
                docs = d.get(key, [])
                if not docs:
                    continue
                doc_info = _best_doc_from_list(docs)
                if doc_info and doc_info.get("url"):
                    lang = doc_info.get("language", "XX")
                    filename = f"{prefix}_{isin}_{lang}.pdf"
                    path = _download_named(doc_info["url"], folder, filename, session)
                    if path:
                        logger.info(f"  → {prefix} gefunden (Key={key}, Lang={lang})")
                        return path
            return None

        result["factsheet"] = _try_keys(_FACTSHEET_KEYS,    "FS")
        result["annual"]    = _try_keys(_ANNUAL_RPT_KEYS,   "AR")
        result["halfyear"]  = _try_keys(_HALFYEAR_RPT_KEYS, "HJB")
        break  # Metadata + Keys wurden von diesem Profil geliefert

    return result


# Reihenfolge der KIID/KID-Dokumenttypen die wir probieren
# PRP = PRIIPs Basisinformationsblatt (bestätigt für IE, LU, AT ISINs)
_KIID_KEYS = ["PRP", "KI", "KID", "WAI", "DICI", "EKI"]


def fetch_kiid(
    isin: str,
    fund_name: str,
    pdf_folder: str,
    delay: float = 1.5,
) -> Optional[DownloadResult]:
    """
    Lädt das KIID/KID-Dokument für eine ISIN von fundinfo.com.

    Caching: Existiert bereits eine Datei KIID_{ISIN}_*.pdf im pdf_folder,
    wird diese zurückgegeben ohne erneuten Download.

    Returns:
        DownloadResult oder None wenn kein KIID gefunden.
    """
    folder = Path(pdf_folder)
    folder.mkdir(parents=True, exist_ok=True)

    # Caching: existierende KIID-Datei für diese ISIN?
    existing = sorted(folder.glob(f"KIID_{isin}_*.pdf"))
    if existing:
        cached = existing[0]
        lang = cached.stem.split("_")[-1] if "_" in cached.stem else ""
        logger.info(f"KIID gecacht: {cached.name}")
        return DownloadResult(
            pdf_path=str(cached),
            pdf_url="",
            language=lang,
            profile="cached",
        )

    session = _get_session()

    # KIID-Suche: nur CH-prof und CH-pub (PRP ist dort verfügbar; spart Zeit)
    kiid_profiles = ["CH-prof", "CH-pub", "LU-prof"]

    for profile in kiid_profiles:
        time.sleep(delay)
        logger.info(f"Suche KIID für {isin} (Profil: {profile})")

        d = _query_api(isin, profile, session)
        if d is None:
            continue

        # Alle verfügbaren Keys loggen (Discovery — hilft beim ersten Test)
        available_keys = [k for k, v in d.items() if v]
        logger.info(f"  fundinfo D-Keys für {isin} @ {profile}: {available_keys}")

        # KIID-Dokumenttyp suchen
        doc_info = None
        found_key = None
        for key in _KIID_KEYS:
            docs = d.get(key, [])
            if docs:
                doc_info = _best_doc_from_list(docs)
                if doc_info and doc_info.get("url"):
                    found_key = key
                    break

        if not doc_info or not doc_info.get("url"):
            logger.debug(f"Kein KIID-Dokument in Profil {profile} (Keys: {available_keys})")
            continue

        logger.info(f"  KIID gefunden (Typ: {found_key}, Sprache: {doc_info['language']})")
        time.sleep(0.5)

        # Direkter Download mit festem Dateinamen (ISIN-basiert, kein Nummernsystem)
        lang = doc_info.get("language", "XX")
        filename = f"KIID_{isin}_{lang}.pdf"
        save_path = folder / filename

        try:
            resp = session.get(doc_info["url"], timeout=60, stream=True)
            resp.raise_for_status()
            chunks = []
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.append(chunk)
            content = b"".join(chunks)

            if not content.startswith(b"%PDF") and b"%PDF" not in content[:1024]:
                logger.warning(f"KIID ist keine PDF: {doc_info['url'][:80]}")
                continue

            size_mb = len(content) / (1024 * 1024)
            if size_mb > 10:
                logger.warning(f"KIID zu gross ({size_mb:.1f} MB), überspringe")
                continue

            with open(save_path, "wb") as f:
                f.write(content)

            logger.info(f"KIID gespeichert: {filename} ({size_mb:.2f} MB)")
            return DownloadResult(
                pdf_path=str(save_path),
                pdf_url=doc_info["url"],
                language=lang,
                profile=profile,
            )

        except requests.RequestException as e:
            logger.error(f"KIID Download-Fehler: {e}")
            if save_path.exists():
                save_path.unlink()
            continue

    logger.warning(f"Kein KIID auf fundinfo.com für ISIN: {isin}")
    return None
