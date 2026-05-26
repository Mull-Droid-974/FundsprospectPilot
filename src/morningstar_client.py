"""
Morningstar Direct Web Services — API-Client.

Authentifizierung via OAuth2 Client Credentials Flow.
Datenabruf via REST API (konfigurierbarer Endpoint).

Endpoint-Konfiguration:
  MORNINGSTAR_TOKEN_URL  — OAuth2 Token-Endpoint (z.B. https://www.morningstar.com/oauth/token)
  MORNINGSTAR_DATA_URL   — Datapoint-API Endpoint (aus Morningstar Direct-Dokumentation)

Die genauen URLs können im Admin-Panel unter "🌟 Morningstar" konfiguriert werden.
"""

import json
import os
from typing import Optional

import requests

from utils import logger

# ─── Verfügbare Datapoints ───────────────────────────────────────────────────
# Format: internal_key → (morningstar_api_field, deutsch_label)
AVAILABLE_DATAPOINTS: dict[str, tuple[str, str]] = {
    "ms_secid":              ("SecId",                        "Morningstar SecId"),
    "ms_name":               ("Name",                         "Wertpapiername"),
    "ms_category":           ("CategoryName",                 "Fondskategorie"),
    "ms_category_id":        ("CategoryId",                   "Kategorie-ID"),
    "ms_asset_class":        ("AssetClassCode",               "Asset-Klasse"),
    "ms_legal_type":         ("LegalType",                    "Rechtsform (UCITS/AIF)"),
    "ms_umbrella":           ("FamilyName",                   "Umbrella / Fund Family"),
    "ms_share_class_type":   ("ShareClassType",               "Anteilsklassen-Typ"),
    "ms_share_class_status": ("Status",                       "Status der Klasse"),
    "ms_inception_date":     ("InceptionDate",                "Auflagedatum"),
    "ms_termination_date":   ("TerminationDate",              "Enddatum"),
    "ms_domicile":           ("DomicileCountryId",            "Domizil"),
    "ms_currency":           ("CurrencyId",                   "Währung"),
    "ms_ongoing_charge":     ("OngoingCharge",                "Laufende Kosten (TER)"),
    "ms_min_investment":     ("MinimumInitialInvestment",     "Mindestanlage"),
    "ms_investor_type":      ("InstitutionalFlag",            "Anlegertyp (Institutional)"),
    "ms_mifid_category":     ("MiFIDCategory",               "MiFID-Kategorie"),
    "ms_rating":             ("RatingOverall",                "Sterne-Rating (1-5)"),
    "ms_risk_rating":        ("RiskScore",                    "Risiko-Score"),
}

# Standard-Auswahl beim ersten Start
DEFAULT_SELECTED: list[str] = [
    "ms_category", "ms_asset_class", "ms_legal_type", "ms_umbrella",
    "ms_share_class_type", "ms_share_class_status", "ms_inception_date",
    "ms_domicile", "ms_currency", "ms_ongoing_charge", "ms_investor_type",
    "ms_mifid_category", "ms_rating",
]

# Batch-Grösse pro API-Aufruf
_BATCH_SIZE = 50


def get_token(client_id: str, client_secret: str, token_url: str) -> str:
    """
    Holt einen OAuth2 Bearer Token via Client Credentials Flow.

    Args:
        client_id:      Morningstar Client ID
        client_secret:  Morningstar Client Secret
        token_url:      OAuth2 Token-Endpoint URL

    Returns:
        Bearer Token (gültig ~60 Minuten)

    Raises:
        ValueError:   bei ungültigen Credentials (401/403)
        RuntimeError: bei anderen API-Fehlern
    """
    if not all([client_id, client_secret, token_url]):
        raise ValueError("Client ID, Client Secret und Token-URL müssen konfiguriert sein.")

    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Verbindungsfehler zum Token-Endpoint: {exc}")

    if resp.status_code in (401, 403):
        raise ValueError(f"Ungültige Morningstar Credentials (HTTP {resp.status_code}).")
    if not resp.ok:
        raise RuntimeError(f"Token-Endpoint Fehler {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    token = data.get("access_token") or data.get("token")
    if not token:
        raise RuntimeError(f"Kein access_token in der Antwort: {list(data.keys())}")
    logger.info("Morningstar OAuth2-Token erhalten.")
    return token


def fetch_datapoints(
    isins: list[str],
    field_keys: list[str],
    token: str,
    data_url: str,
) -> dict[str, dict]:
    """
    Ruft Datapoints für eine Liste von ISINs ab.

    API-Format: POST {data_url} mit JSON-Body.
    Die genaue Request-Struktur ist auf die Morningstar Direct Web Services API
    abgestimmt. Bitte Endpoint und ggf. Request-Format gemäss Ihrer
    Morningstar Direct Dokumentation anpassen.

    Returns:
        {isin: {internal_key: value, ..., "ms_raw_json": "..."}}
    """
    if not all([isins, field_keys, token, data_url]):
        return {}

    # Morningstar API-Feldnamen aus den internen Keys ableiten
    api_fields = [
        AVAILABLE_DATAPOINTS[k][0]
        for k in field_keys
        if k in AVAILABLE_DATAPOINTS
    ]
    # SecId immer mit anfordern (für ms_secid)
    if "SecId" not in api_fields:
        api_fields.insert(0, "SecId")

    results: dict[str, dict] = {}

    for i in range(0, len(isins), _BATCH_SIZE):
        batch = isins[i : i + _BATCH_SIZE]

        # ── Request-Body ───────────────────────────────────────────────
        # Standard-Format für Morningstar Direct Web Services Export API.
        # Passen Sie diesen Block an Ihre spezifische API-Subscription an.
        payload = {
            "data": {
                "instruments": [
                    {"identifier": isin, "identifierType": "ISIN"}
                    for isin in batch
                ]
            },
            "dataPoints": api_fields,
        }

        try:
            resp = requests.post(
                data_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Verbindungsfehler zum Daten-Endpoint: {exc}")

        if resp.status_code == 401:
            raise ValueError("Token abgelaufen oder ungültig — bitte neu verbinden.")
        if not resp.ok:
            raise RuntimeError(f"Daten-API Fehler {resp.status_code}: {resp.text[:300]}")

        try:
            raw = resp.json()
        except Exception:
            raise RuntimeError(f"Ungültige JSON-Antwort: {resp.text[:200]}")

        # ── Antwort parsen ─────────────────────────────────────────────
        # Erwartet: {"data": [{"isin": "...", "SecId": "...", ...}, ...]}
        # Passen Sie diese Struktur an Ihre tatsächliche API-Antwort an.
        items = _extract_items(raw, batch)
        for item in items:
            isin = _find_isin(item, batch)
            if not isin:
                continue
            parsed = _map_fields(item, field_keys)
            parsed["ms_raw_json"] = json.dumps(item, ensure_ascii=False)[:4000]
            results[isin] = parsed

    return results


def _extract_items(raw: dict | list, batch: list[str]) -> list[dict]:
    """Extrahiert die Datensatz-Liste aus der API-Antwort (flexibles Format)."""
    if isinstance(raw, list):
        return raw
    for key in ("data", "results", "securities", "instruments", "rows"):
        if key in raw and isinstance(raw[key], list):
            return raw[key]
    # Flache Antwort: {"ISIN1": {...}, "ISIN2": {...}}
    if all(k.upper() in (i.upper() for i in batch) for k in list(raw.keys())[:2]):
        return [{"_isin": k, **v} for k, v in raw.items()]
    return []


def _find_isin(item: dict, batch: list[str]) -> str:
    """Sucht die ISIN im Datensatz (verschiedene mögliche Feldnamen)."""
    for key in ("ISIN", "isin", "Isin", "identifier", "_isin"):
        val = item.get(key, "")
        if val and val.upper() in (i.upper() for i in batch):
            return val.upper()
    return ""


def _map_fields(item: dict, field_keys: list[str]) -> dict:
    """Mappt API-Felder auf interne Keys."""
    result = {}
    for key in field_keys:
        if key not in AVAILABLE_DATAPOINTS:
            continue
        api_field, _ = AVAILABLE_DATAPOINTS[key]
        # Verschiedene Gross-/Kleinschreibungs-Varianten versuchen
        val = item.get(api_field) or item.get(api_field.lower()) or item.get(api_field.upper()) or ""
        result[key] = str(val) if val is not None else ""
    return result
