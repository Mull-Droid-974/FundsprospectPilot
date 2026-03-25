"""Fallback Web-Suche wenn PDF-Analyse unklar ist."""

from typing import Optional

from utils import logger


def search_fund_info(isin: str, fund_name: str, max_results: int = 5) -> Optional[str]:
    """
    Sucht im Web nach Informationen zur Anteilsklasse.
    Verwendet DuckDuckGo (kein API-Key nötig).

    Returns:
        Gefundene Textausschnitte als String, oder None.
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.warning("duckduckgo-search nicht installiert, Web-Suche nicht verfügbar")
        return None

    queries = [
        f"{isin} institutional retail investor type",
        f"{fund_name} {isin} anlegertyp institutional",
        f"{isin} share class investor eligibility",
    ]

    results = []
    try:
        with DDGS() as ddgs:
            for query in queries[:2]:  # Nur 2 Queries um Rate Limits zu vermeiden
                try:
                    hits = list(ddgs.text(query, max_results=max_results))
                    for hit in hits:
                        snippet = hit.get("body", "")
                        title = hit.get("title", "")
                        if snippet and any(
                            kw in (snippet + title).lower()
                            for kw in ["institutional", "retail", "anleger", "investor"]
                        ):
                            results.append(f"[{title}]\n{snippet}")
                except Exception as e:
                    logger.debug(f"DuckDuckGo Suche fehlgeschlagen: {e}")
                    break

    except Exception as e:
        logger.warning(f"Web-Suche fehlgeschlagen für {isin}: {e}")
        return None

    if results:
        combined = "\n\n---\n\n".join(results[:3])
        logger.info(f"Web-Suche: {len(results)} Treffer für {isin}")
        return combined[:5000]

    return None
