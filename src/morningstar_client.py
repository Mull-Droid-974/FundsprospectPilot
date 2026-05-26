"""
Morningstar MCP Client.

Verbindet mit dem offiziellen Morningstar MCP Server (mcp.morningstar.com)
via Model Context Protocol (JSON-RPC über HTTP, Streamable-HTTP-Transport).
Authentifizierung via OAuth2 Resource Owner Password Grant (E-Mail + Passwort).

MCP-Endpoint ist fest: https://mcp.morningstar.com/mcp
Token-URL wird automatisch via Well-Known-Discovery ermittelt.
"""

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from utils import logger

# Fester MCP-Endpoint — kein Admin-Feld notwendig
MCP_ENDPOINT = os.getenv("MORNINGSTAR_MCP_URL", "https://mcp.morningstar.com/mcp")
MCP_BASE     = "https://mcp.morningstar.com"   # Basis für Well-Known-Discovery

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


# ─── OAuth2 Discovery & Browser-Login ───────────────────────────────────────

def discover_auth_endpoints(mcp_base: str = MCP_BASE) -> dict:
    """
    Ermittelt alle OAuth2-Endpoints via MCP Well-Known-Discovery (RFC 9728 → RFC 8414).

    Returns:
        {
          "issuer": str,
          "authorization_endpoint": str,
          "token_endpoint": str,
          "registration_endpoint": str,   # leer wenn nicht vorhanden
        }
    Raises:
        RuntimeError: wenn Discovery fehlschlägt
    """
    # Schritt 1: Protected Resource Metadata → Issuer-URL
    issuer = None
    for path in ["/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-protected-resource"]:
        try:
            resp = requests.get(urljoin(mcp_base, path), timeout=10)
            if resp.ok:
                meta = resp.json()
                servers = meta.get("authorization_servers") or []
                if servers:
                    entry = servers[0]
                    issuer = (entry.get("issuer") or entry) if isinstance(entry, dict) else entry
                    break
        except Exception:
            continue

    if not issuer:
        raise RuntimeError(
            "MCP-Discovery fehlgeschlagen: /.well-known/oauth-protected-resource "
            "lieferte keinen authorization_servers-Eintrag."
        )

    # Schritt 2: Authorization Server Metadata → alle Endpoints
    for path in ["/.well-known/oauth-authorization-server",
                 "/.well-known/openid-configuration"]:
        try:
            resp = requests.get(urljoin(issuer, path), timeout=10)
            if resp.ok:
                meta = resp.json()
                if meta.get("token_endpoint"):
                    logger.info(f"Morningstar Auth-Endpoints entdeckt (Issuer: {issuer})")
                    return {
                        "issuer":                  issuer,
                        "authorization_endpoint":  meta.get("authorization_endpoint", ""),
                        "token_endpoint":          meta.get("token_endpoint", ""),
                        "registration_endpoint":   meta.get("registration_endpoint", ""),
                    }
        except Exception:
            continue

    raise RuntimeError(
        f"Auth-Server-Metadaten nicht gefunden (Issuer: {issuer})"
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _try_dynamic_registration(registration_endpoint: str) -> str:
    """Dynamische Client-Registrierung (RFC 7591). Gibt client_id zurück oder ''."""
    if not registration_endpoint:
        return ""
    try:
        resp = requests.post(
            registration_endpoint,
            json={
                "client_name":                "FundProspektPilot",
                "redirect_uris":              ["http://localhost"],
                "grant_types":                ["authorization_code"],
                "response_types":             ["code"],
                "token_endpoint_auth_method": "none",
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        if resp.ok:
            return resp.json().get("client_id", "")
    except Exception:
        pass
    return ""


def login_via_browser(
    auth_url: str,
    token_url: str,
    client_id: str = "",
    timeout: int = 300,
) -> dict:
    """
    OAuth2 Authorization Code + PKCE via Browser-Login.

    Ablauf:
      1. Lokaler Callback-Server auf zufälligem Port starten
      2. Browser mit Auth-URL öffnen → Nutzer meldet sich auf Morningstar-Seite an
      3. Browser leitet zu localhost/callback → Code empfangen
      4. Code gegen Access-Token tauschen

    Returns:
        {"access_token": ..., "refresh_token": ..., "expires_in": ...}
    Raises:
        RuntimeError / ValueError bei Fehler oder Timeout
    """
    port         = _find_free_port()
    redirect_uri = f"http://localhost:{port}/callback"
    state        = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()

    result: dict = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs        = parse_qs(urlparse(self.path).query)
            code      = (qs.get("code")  or [""])[0]
            got_state = (qs.get("state") or [""])[0]
            error     = (qs.get("error") or [""])[0]

            if code and got_state == state:
                result["code"] = code
                body = "<h2>Anmeldung erfolgreich — dieses Fenster kann geschlossen werden.</h2>"
            else:
                result["error"] = error or "state_mismatch"
                body = "<h2>Anmeldung fehlgeschlagen oder abgebrochen.</h2>"

            body_bytes = f"<html><body>{body}</body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", port), _Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    params: dict = {
        "response_type":         "code",
        "redirect_uri":          redirect_uri,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    if client_id:
        params["client_id"] = client_id

    webbrowser.open(f"{auth_url}?{urlencode(params)}")
    logger.info("Browser für Morningstar-Login geöffnet.")

    if not done.wait(timeout=timeout):
        server.server_close()
        raise RuntimeError(
            f"Timeout: Browser-Login nicht innerhalb von {timeout // 60} Min. abgeschlossen."
        )
    server.server_close()

    if "error" in result:
        raise ValueError(f"Login abgebrochen: {result['error']}")

    # Code gegen Token tauschen
    data: dict = {
        "grant_type":   "authorization_code",
        "code":         result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_id:
        data["client_id"] = client_id

    try:
        resp = requests.post(
            token_url,
            data=data,
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Verbindungsfehler beim Token-Tausch: {exc}")

    if resp.status_code in (400, 401):
        raise ValueError(f"Token-Tausch abgelehnt (HTTP {resp.status_code}): {resp.text[:200]}")
    if not resp.ok:
        raise RuntimeError(f"Token-Endpoint Fehler {resp.status_code}: {resp.text[:200]}")

    tokens = resp.json()
    if not tokens.get("access_token"):
        raise RuntimeError(f"Kein access_token in der Antwort: {list(tokens.keys())}")

    logger.info("Morningstar: Browser-Login erfolgreich, Token erhalten.")
    return tokens


def refresh_access_token(refresh_token_val: str, token_url: str, client_id: str = "") -> dict:
    """
    Erneuert den Access Token via Refresh Token.
    Returns dict mit access_token (und ggf. neuem refresh_token).
    """
    data: dict = {"grant_type": "refresh_token", "refresh_token": refresh_token_val}
    if client_id:
        data["client_id"] = client_id
    try:
        resp = requests.post(
            token_url, data=data,
            headers={"Accept": "application/json"}, timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Verbindungsfehler beim Token-Refresh: {exc}")
    if not resp.ok:
        raise RuntimeError(f"Token-Refresh fehlgeschlagen (HTTP {resp.status_code})")
    return resp.json()


# Rückwärtskompatibilität — wird intern nicht mehr verwendet
discover_token_url = lambda mcp_base=MCP_BASE: discover_auth_endpoints(mcp_base)["token_endpoint"]


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
