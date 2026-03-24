"""
fundinfo.com Scraping und PDF-Download.

Strategie:
1. Suche auf fundinfo.com nach der ISIN
2. Finde den Link zum Verkaufsprospekt
3. Lade die PDF herunter
"""

import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from utils import logger, build_pdf_filename, get_next_pdf_number

# Browser-ähnliche Headers, um 403 zu vermeiden
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 30  # Sekunden
REQUEST_DELAY = 1.5   # Sekunden zwischen Requests


def _get_session() -> requests.Session:
    """Erstellt eine requests.Session mit Browser-Headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def find_prospectus_url(isin: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """
    Sucht den Verkaufsprospekt für eine ISIN auf fundinfo.com.

    Returns:
        URL des PDF oder None falls nicht gefunden.
    """
    if session is None:
        session = _get_session()

    strategies = [
        _try_direct_isin_url,
        _try_search_url,
    ]

    for strategy in strategies:
        try:
            url = strategy(isin, session)
            if url:
                logger.info(f"Prospekt gefunden für {isin}: {url[:80]}...")
                return url
        except Exception as e:
            logger.debug(f"Strategie {strategy.__name__} fehlgeschlagen für {isin}: {e}")

    logger.warning(f"Kein Prospekt auf fundinfo.com gefunden für ISIN: {isin}")
    return None


def _try_direct_isin_url(isin: str, session: requests.Session) -> Optional[str]:
    """Versucht den direkten Pfad: fundinfo.com/de/{isin}"""
    base_urls = [
        f"https://fundinfo.com/de/{isin}",
        f"https://fundinfo.com/en/{isin}",
        f"https://www.fundinfo.com/de/{isin}",
    ]

    for base_url in base_urls:
        try:
            resp = session.get(base_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                pdf_url = _extract_prospectus_link(resp.text, base_url)
                if pdf_url:
                    return pdf_url
        except requests.RequestException:
            continue

    return None


def _try_search_url(isin: str, session: requests.Session) -> Optional[str]:
    """Sucht via fundinfo.com Suchfunktion."""
    search_urls = [
        f"https://fundinfo.com/de/search?q={quote(isin)}",
        f"https://fundinfo.com/search?q={quote(isin)}",
    ]

    for search_url in search_urls:
        try:
            resp = session.get(search_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Ersten Suchergebnis-Link finden
            fund_link = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if isin.lower() in href.lower() or (
                    any(x in href for x in ["/fund/", "/fonds/", "/de/", "/en/"])
                    and len(href) > 20
                ):
                    fund_link = href
                    break

            if fund_link:
                if not fund_link.startswith("http"):
                    fund_link = urljoin("https://fundinfo.com", fund_link)

                time.sleep(0.5)
                resp2 = session.get(fund_link, timeout=REQUEST_TIMEOUT)
                if resp2.status_code == 200:
                    pdf_url = _extract_prospectus_link(resp2.text, fund_link)
                    if pdf_url:
                        return pdf_url

        except requests.RequestException:
            continue

    return None


def _extract_prospectus_link(html: str, base_url: str) -> Optional[str]:
    """Extrahiert den Link zum Verkaufsprospekt aus einer HTML-Seite."""
    soup = BeautifulSoup(html, "html.parser")

    # Deutsche und englische Schlüsselwörter für Prospekte
    keywords = [
        "verkaufsprospekt",
        "sales prospectus",
        "prospectus",
        "prospekt",
        "offering document",
    ]

    # Alle Links durchsuchen
    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(strip=True).lower()

        # PDF-Link mit passendem Text?
        if href.lower().endswith(".pdf"):
            if any(kw in link_text for kw in keywords):
                if not href.startswith("http"):
                    href = urljoin(base_url, href)
                return href

        # Link-Text enthält Prospekt-Keyword?
        if any(kw in link_text for kw in keywords):
            if ".pdf" in href.lower() or "document" in href.lower():
                if not href.startswith("http"):
                    href = urljoin(base_url, href)
                return href

    # Fallback: Irgendein PDF-Link auf der Seite
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") and len(href) > 20:
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            return href

    return None


def download_pdf(
    url: str,
    pdf_folder: str,
    fund_name: str,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """
    Lädt eine PDF herunter und speichert sie mit 5-stelliger Nummer.

    Returns:
        Pfad zur gespeicherten PDF oder None bei Fehler.
    """
    if session is None:
        session = _get_session()

    folder = Path(pdf_folder)
    folder.mkdir(parents=True, exist_ok=True)

    number = get_next_pdf_number(pdf_folder)
    filename = build_pdf_filename(number, fund_name)
    save_path = folder / filename

    try:
        logger.info(f"Lade PDF herunter: {url[:80]}...")
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()

        # Prüfen ob wirklich PDF
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            logger.warning(f"Kein PDF-Content-Type: {content_type}")

        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        size_kb = save_path.stat().st_size / 1024
        logger.info(f"PDF gespeichert: {filename} ({size_kb:.0f} KB)")
        return str(save_path)

    except requests.RequestException as e:
        logger.error(f"Download-Fehler für {url}: {e}")
        if save_path.exists():
            save_path.unlink()
        return None


def fetch_prospectus(
    isin: str,
    fund_name: str,
    pdf_folder: str,
    delay: float = REQUEST_DELAY,
) -> Optional[str]:
    """
    Kompletter Workflow: Suche + Download.

    Returns:
        Lokaler PDF-Pfad oder None falls fehlgeschlagen.
    """
    session = _get_session()

    # Kurze Pause zwischen Requests (Rate Limiting)
    time.sleep(delay)

    pdf_url = find_prospectus_url(isin, session)
    if not pdf_url:
        return None

    time.sleep(0.5)

    return download_pdf(pdf_url, pdf_folder, fund_name, session)
