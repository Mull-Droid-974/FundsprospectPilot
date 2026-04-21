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
PROFILES = ["CH-prof", "CH-pub", "DE-prof", "LU-prof", "AT-prof"]

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
            "subfonds_code":     s.get("OFST900171", ""),
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
