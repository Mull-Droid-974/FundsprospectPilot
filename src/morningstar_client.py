"""
Morningstar MCP Client.

Verbindet mit dem offiziellen Morningstar MCP Server (mcp.morningstar.com)
via Model Context Protocol (JSON-RPC über HTTP, Streamable-HTTP-Transport).
Authentifizierung via OAuth2 Client Credentials Flow.

Endpoint ist fest: https://mcp.morningstar.com/mcp
Token-URL muss im Admin-Panel konfiguriert werden.
"""

import json
import os
from typing import Optional

import requests

from utils import logger

# Fester MCP-Endpoint — kein Admin-Feld notwendig
MCP_ENDPOINT = os.getenv("MORNINGSTAR_MCP_URL", "https://mcp.morningstar.com/mcp")

# ─── Verfügbare Datapoints ───────────────────────────────────────────────────
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
    "ms_mifid_category":     ("MiFIDCategory",                "MiFID-Kategorie"),
    "ms_rating":             ("RatingOverall",                "Sterne-Rating (1-5)"),
    "ms_risk_rating":        ("RiskScore",                    "Risiko-Score"),
}

DEFAULT_SELECTED: list[str] = [
    "ms_category", "ms_asset_class", "ms_legal_type", "ms_umbrella",
    "ms_share_class_type", "ms_share_class_status", "ms_inception_date",
    "ms_domicile", "ms_currency", "ms_ongoing_charge", "ms_investor_type",
    "ms_mifid_category", "ms_rating",
]

_BATCH_SIZE = 20


# ─── OAuth2 ──────────────────────────────────────────────────────────────────

def get_token(client_id: str, client_secret: str, token_url: str) -> str:
    """
    Holt einen OAuth2 Bearer Token via Client Credentials Flow.

    Raises:
        ValueError:   bei ungültigen Credentials (401/403)
        RuntimeError: bei anderen Fehlern
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


# ─── MCP-Session ─────────────────────────────────────────────────────────────

class _MCPSession:
    """
    Einfache MCP-Session über Streamable-HTTP-Transport (JSON-RPC 2.0).

    Protokoll-Ablauf:
      1. initialize-Request → Antwort + Mcp-Session-Id Header
      2. notifications/initialized-Notification (kein Response)
      3. tools/list → verfügbare Tools
      4. tools/call → Tool aufrufen
    """

    def __init__(self, token: str, endpoint: str = MCP_ENDPOINT):
        self._endpoint = endpoint
        self._session_id: Optional[str] = None
        self._req_id = 0
        self._base_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/event-stream",
        }

    def _headers(self) -> dict:
        h = dict(self._base_headers)
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post(self, method: str, params: dict, *, expect_response: bool = True):
        """Sendet eine JSON-RPC-Nachricht. Gibt None zurück bei Notifications."""
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if expect_response:
            body["id"] = self._next_id()

        try:
            resp = requests.post(
                self._endpoint,
                json=body,
                headers=self._headers(),
                timeout=60,
                stream=True,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"MCP-Verbindungsfehler: {exc}")

        # Session-ID speichern
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

        if not expect_response:
            return None

        if resp.status_code == 401:
            raise ValueError("MCP: Token abgelaufen oder ungültig.")
        if not resp.ok:
            raise RuntimeError(f"MCP HTTP {resp.status_code}: {resp.text[:300]}")

        ct = resp.headers.get("content-type", "")
        if "event-stream" in ct:
            return self._read_sse(resp)

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"MCP: Ungültige JSON-Antwort: {resp.text[:200]}")

        if "error" in data:
            raise RuntimeError(f"MCP-Fehler: {data['error']}")
        return data.get("result", {})

    def _read_sse(self, resp) -> dict:
        """Liest einen SSE-Stream und gibt das erste result-Event zurück."""
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "error" in data:
                raise RuntimeError(f"MCP-Fehler: {data['error']}")
            if "result" in data:
                return data["result"]
        raise RuntimeError("MCP: Kein Ergebnis im SSE-Stream empfangen.")

    def initialize(self):
        result = self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "FundProspektPilot", "version": "1.0"},
        })
        # Pflicht-Notification nach erfolgreicher Initialisierung
        self._post("notifications/initialized", {}, expect_response=False)
        return result

    def list_tools(self) -> list[dict]:
        result = self._post("tools/list", {})
        return result.get("tools", []) if isinstance(result, dict) else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {}


# ─── Öffentliche API ─────────────────────────────────────────────────────────

def list_mcp_tools(token: str) -> list[dict]:
    """
    Listet alle verfügbaren MCP-Tools des Morningstar-Servers auf.
    Gibt [{"name": ..., "description": ..., "schema": ...}, ...] zurück.
    """
    sess = _MCPSession(token)
    sess.initialize()
    raw_tools = sess.list_tools()
    return [
        {
            "name":        t.get("name", ""),
            "description": t.get("description", ""),
            "schema":      t.get("inputSchema", {}),
        }
        for t in raw_tools
    ]


def fetch_datapoints(
    isins: list[str],
    field_keys: list[str],
    token: str,
    data_url: str = None,       # veraltet, wird ignoriert
) -> dict[str, dict]:
    """
    Ruft Morningstar-Daten für eine Liste von ISINs via MCP ab.

    Returns:
        {isin: {internal_key: value, ..., "ms_raw_json": "..."}}
    """
    if not all([isins, field_keys, token]):
        return {}

    api_fields = [
        AVAILABLE_DATAPOINTS[k][0]
        for k in field_keys
        if k in AVAILABLE_DATAPOINTS
    ]

    sess = _MCPSession(token)
    sess.initialize()

    # Tools entdecken
    raw_tools = sess.list_tools()
    tools = {t.get("name", ""): t for t in raw_tools}
    logger.info(f"Morningstar MCP Tools: {list(tools.keys())}")

    tool_name, build_args = _pick_tool(tools, api_fields)
    if not tool_name:
        raise RuntimeError(
            f"Kein passendes ISIN-Tool gefunden. "
            f"Verfügbar: {list(tools.keys())}"
        )
    logger.info(f"Verwende MCP-Tool: {tool_name}")

    results: dict[str, dict] = {}
    for isin in isins:
        try:
            raw = sess.call_tool(tool_name, build_args(isin))
            item = _extract_item(raw, isin)
            if item:
                parsed = _map_fields(item, field_keys)
                parsed["ms_raw_json"] = json.dumps(item, ensure_ascii=False)[:4000]
                results[isin.upper()] = parsed
        except Exception as exc:
            logger.warning(f"MCP-Abruf fehlgeschlagen für {isin}: {exc}")

    return results


# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

def _pick_tool(tools: dict, api_fields: list) -> tuple:
    """
    Wählt das passende MCP-Tool für ISIN-Suche.
    Returns: (tool_name, args_builder_fn) oder (None, None)
    """
    # Bekannte Kandidaten (Morningstar-Naming-Konventionen)
    _CANDIDATES = [
        "get_security_data",
        "get_security_details",
        "get_security",
        "get_fund",
        "get_fund_data",
        "getSecurityDatapoints",
        "search_security",
        "search_securities",
    ]

    def _builder_for(props: dict):
        if "isin" in props:
            return lambda isin: {"isin": isin}
        if "identifier" in props:
            return lambda isin: {"identifier": isin, "identifierType": "ISIN"}
        if "securityId" in props:
            return lambda isin: {"securityId": isin, "idType": "ISIN"}
        # Ersten String-Parameter nehmen
        for k, v in props.items():
            if isinstance(v, dict) and v.get("type") == "string":
                return lambda isin, key=k: {key: isin}
        return None

    for name in _CANDIDATES:
        if name not in tools:
            continue
        schema = tools[name].get("inputSchema") or {}
        props  = schema.get("properties") or {}
        builder = _builder_for(props)
        if builder:
            return name, builder

    # Fallback: jedes Tool mit "isin" im Schema
    for name, tool in tools.items():
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        isin_keys = [k for k in props if "isin" in k.lower()]
        if isin_keys:
            k = isin_keys[0]
            return name, lambda isin, key=k: {key: isin}

    return None, None


def _extract_item(raw: dict, isin: str) -> Optional[dict]:
    """Extrahiert einen Datensatz aus der MCP-Tool-Antwort."""
    if not isinstance(raw, dict):
        return None

    # content-Liste (Standard-MCP-Format)
    content = raw.get("content") or []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("data") or ""
        if isinstance(text, str) and text:
            try:
                parsed = json.loads(text)
            except Exception:
                return {"raw": text}
            if isinstance(parsed, list) and parsed:
                return parsed[0]
            if isinstance(parsed, dict):
                for key in ("data", "security", "fund", "result", "security_data"):
                    sub = parsed.get(key)
                    if isinstance(sub, list) and sub:
                        return sub[0]
                    if isinstance(sub, dict):
                        return sub
                return parsed

    # Flache Antwort ohne content-Wrapper
    if any(k in raw for k in ("SecId", "CategoryName", "Name", "isin", "ISIN")):
        return raw

    return None


def _map_fields(item: dict, field_keys: list[str]) -> dict:
    """Mappt Morningstar-API-Felder auf interne Keys."""
    result = {}
    for key in field_keys:
        if key not in AVAILABLE_DATAPOINTS:
            continue
        api_field, _ = AVAILABLE_DATAPOINTS[key]
        val = (item.get(api_field)
               or item.get(api_field.lower())
               or item.get(api_field.upper())
               or "")
        result[key] = str(val) if val is not None else ""
    return result
