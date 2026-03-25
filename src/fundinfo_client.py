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


def _discover_pdf_url(isin: str, profile: str, session: requests.Session) -> Optional[dict]:
    """
    Ruft die fundinfo.com JSON-API auf und findet den Verkaufsprospekt.

    Returns:
        {"url": "...", "language": "DE", "date": "2024-01-01"} oder None
    """
    api_url = f"https://www.fundinfo.com/en/{profile}/LandingPage/Data"
    params = {
        "skip": 0,
        "query": isin,
        "orderdirection": "desc",
    }

    try:
        resp = session.get(api_url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # Dokumente aus der Antwort extrahieren
        items = data.get("Data", [])
        if not items:
            return None

        # Verkaufsprospekte (Typ "PR") aus dem ersten Ergebnis
        docs = items[0].get("D", {}).get("PR", [])
        if not docs:
            return None

        # Nur aktive Dokumente, nach Datum sortiert, Sprache priorisiert
        active = [d for d in docs if d.get("Active", True)]
        if not active:
            active = docs  # Fallback: alle nehmen

        def sort_key(doc):
            lang = doc.get("Language", "XX")
            lang_rank = LANG_PREFERENCE.index(lang) if lang in LANG_PREFERENCE else 99
            date_str = doc.get("Date", "1900-01-01")
            return (lang_rank, date_str)  # niedrigerer Rang = bevorzugt

        best = sorted(active, key=sort_key)[0]

        return {
            "url": best.get("Url", ""),
            "language": best.get("Language", ""),
            "date": best.get("Date", ""),
        }

    except requests.RequestException as e:
        logger.debug(f"fundinfo API Fehler (Profil {profile}): {e}")
        return None
    except (KeyError, IndexError, ValueError) as e:
        logger.debug(f"fundinfo Antwort Parse-Fehler (Profil {profile}): {e}")
        return None


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
