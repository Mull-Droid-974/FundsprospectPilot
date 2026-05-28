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

# Morningstar MCP Datapoint-IDs für unsere internen Felder (via id-lookup-tool ermittelt)
_DP_ID_MAP: dict[str, str] = {
    "ms_category":           "OF003",  # Morningstar Category
    "ms_rating":             "RR01Y",  # Morningstar Rating Overall
    "ms_investor_type":      "OS00N",  # Institutional flag
    "ms_inception_date":     "OS00F",  # Inception Date
    "ms_ongoing_charge":     "OS05P",  # Ongoing Charge / TER
    "ms_min_investment":     "OS388",  # Minimum Investment (Base Currency)
    "ms_domicile":           "LS017",  # Domicile
    "ms_currency":           "LS468",  # Portfolio Currency
    "ms_share_class_status": "OS999",  # Status (Active/Inactive)
    "ms_share_class_type":   "LS012",  # Share Class Type
    "ms_risk_rating":        "RR04W",  # Morningstar Risk Rating Overall
    "ms_name":               "OS01W",  # Fund Name
}


# ─── OAuth2 Discovery & Browser-Login ───────────────────────────────────────

_MS_AUTH_BASE = "https://login-prod.morningstar.com"   # bekannter Auth0-Server


def discover_auth_endpoints(mcp_base: str = MCP_BASE) -> dict:
    """
    Ermittelt alle OAuth2-Endpoints für den Morningstar MCP-Server.

    Strategie (erste die funktioniert):
      1. RFC 9728 Well-Known (oauth-protected-resource)
      2. WWW-Authenticate-Header des MCP-Endpoints
      3. Bekannter Fallback: login-prod.morningstar.com (Auth0)
      4. OpenID Configuration des ermittelten Issuers

    Returns:
        {"issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"}
    """
    import re

    issuer = None

    # Schritt 1: Protected Resource Metadata (RFC 9728)
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
                    logger.info(f"Morningstar Issuer via RFC 9728: {issuer}")
                    break
        except Exception:
            continue

    # Schritt 2: WWW-Authenticate-Header des MCP-Endpoints auslesen
    if not issuer:
        try:
            resp = requests.get(MCP_ENDPOINT, timeout=10)
            auth_hdr = resp.headers.get("WWW-Authenticate", "")
            m = re.search(r'as_uri="([^"]+)"', auth_hdr)
            if m:
                issuer = m.group(1)
                logger.info(f"Morningstar Issuer via WWW-Authenticate: {issuer}")
        except Exception:
            pass

    # Schritt 3: Bekannter Morningstar-Auth0-Server als Fallback
    if not issuer:
        issuer = _MS_AUTH_BASE
        logger.warning(f"Discovery fehlgeschlagen — verwende bekannten Fallback: {issuer}")

    # Schritt 4: OpenID Configuration / OAuth AS Metadata
    for path in ["/.well-known/openid-configuration",
                 "/.well-known/oauth-authorization-server"]:
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

    # Schritt 5: Auth0-Standard-Endpoints direkt konstruieren (Fallback)
    logger.warning(f"OpenID Configuration nicht erreichbar — konstruiere Auth0-Endpoints für {issuer}")
    return {
        "issuer":                  issuer,
        "authorization_endpoint":  f"{issuer.rstrip('/')}/authorize",
        "token_endpoint":          f"{issuer.rstrip('/')}/oauth/token",
        "registration_endpoint":   f"{issuer.rstrip('/')}/oidc/register",
    }


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _try_dynamic_registration(registration_endpoint: str, redirect_uri: str) -> tuple[str, str]:
    """
    Dynamische Client-Registrierung (RFC 7591).
    Returns: (client_id, error_detail) — einer davon ist immer leer.
    """
    if not registration_endpoint:
        return "", "Kein registration_endpoint bekannt"
    try:
        resp = requests.post(
            registration_endpoint,
            json={
                "client_name":                "FundProspektPilot",
                "redirect_uris":              [redirect_uri],
                "grant_types":                ["authorization_code", "password", "refresh_token"],
                "response_types":             ["code"],
                "token_endpoint_auth_method": "none",
                "application_type":           "native",
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        if resp.ok:
            cid = resp.json().get("client_id", "")
            if cid:
                logger.info(f"Dynamic Registration erfolgreich: client_id={cid[:8]}...")
                return cid, ""
            return "", f"Registrierung OK aber kein client_id in Antwort: {resp.text[:300]}"
        return "", f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        return "", f"Verbindungsfehler: {exc}"


def login_via_browser(
    auth_url: str,
    token_url: str,
    client_id: str = "",
    registration_endpoint: str = "",
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

    # Dynamic Registration mit exakter redirect_uri (falls noch keine client_id)
    reg_error = ""
    if not client_id and registration_endpoint:
        client_id, reg_error = _try_dynamic_registration(registration_endpoint, redirect_uri)
        if client_id:
            logger.info(f"Verwende dynamisch registrierte client_id: {client_id[:8]}...")
        else:
            logger.warning(f"Dynamic Registration fehlgeschlagen: {reg_error}")

    if not client_id:
        raise RuntimeError(
            f"Kein OAuth2 Client-ID verfügbar.\n\n"
            f"Registrierungsversuch: {reg_error or 'kein registration_endpoint'}\n\n"
            f"Lösung: Eine Client-ID von Morningstar (developer.morningstar.com) "
            f"beziehen und im Admin-Panel eintragen."
        )

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
    if client_id:
        tokens["_client_id"] = client_id
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


def login_with_password(
    username: str,
    password: str,
    token_url: str,
    client_id: str = "",
) -> dict:
    """
    OAuth2 Resource Owner Password Credentials Grant (RFC 6749 §4.3).

    Meldet sich direkt mit E-Mail + Passwort an — kein Browser nötig.
    Returns: {"access_token": ..., "refresh_token": ..., "expires_in": ...}
    Raises: ValueError bei falschen Credentials, RuntimeError bei Verbindungsfehlern.
    """
    if not username or not password:
        raise ValueError("E-Mail und Passwort dürfen nicht leer sein.")
    if not token_url:
        raise RuntimeError("Kein token_url angegeben.")

    data: dict = {
        "grant_type": "password",
        "username":   username,
        "password":   password,
        "scope":      "openid profile",
    }
    if client_id:
        data["client_id"] = client_id

    try:
        resp = requests.post(
            token_url, data=data,
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Verbindungsfehler beim Login: {exc}")

    if resp.status_code in (400, 401):
        try:
            body = resp.json()
            err  = body.get("error_description") or body.get("error") or resp.text[:200]
        except Exception:
            err = resp.text[:200]
        raise ValueError(f"Login fehlgeschlagen: {err}")

    if resp.status_code == 400:
        try:
            body = resp.json()
            if body.get("error") == "unsupported_grant_type":
                raise ValueError(
                    "Morningstar unterstützt ROPC nicht. "
                    "Bitte den Browser-Login verwenden."
                )
        except ValueError:
            raise
        except Exception:
            pass

    if not resp.ok:
        raise RuntimeError(f"Token-Endpoint Fehler {resp.status_code}: {resp.text[:200]}")

    tokens = resp.json()
    if not tokens.get("access_token"):
        raise RuntimeError(
            f"Kein access_token in der Antwort: {list(tokens.keys())}\n"
            f"Möglicherweise unterstützt der Server kein Password-Grant."
        )

    logger.info("Morningstar: Password-Login erfolgreich, Token erhalten.")
    return tokens


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
        """Liest einen SSE-Stream — akkumuliert multi-line data:-Chunks pro Event."""
        data_chunks: list[str] = []
        for line in resp.iter_lines(decode_unicode=True):
            if line.startswith("data:"):
                data_chunks.append(line[5:].strip())
            elif not line and data_chunks:
                raw = "".join(data_chunks)
                data_chunks = []
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
        # Stream-Ende ohne Leerzeile (einige Server)
        if data_chunks:
            try:
                data = json.loads("".join(data_chunks))
                if "result" in data:
                    return data["result"]
            except Exception:
                pass
        raise RuntimeError("MCP: Kein Ergebnis im SSE-Stream empfangen.")

    def initialize(self):
        result = self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "FundProspektPilot", "version": "1.0"},
        })
        self._post("notifications/initialized", {}, expect_response=False)
        return result

    def list_tools(self) -> list[dict]:
        result = self._post("tools/list", {})
        return result.get("tools", []) if isinstance(result, dict) else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict):
            return result.get("structuredContent") or result
        return {}


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
    Ruft Morningstar-Daten für eine Liste von ISINs via MCP ab (2-Schritt-Flow):
      1. morningstar-id-lookup-tool: ISIN → Morningstar investment_id
      2. morningstar-data-tool:      investment_id + datapoint_ids → Werte

    Returns:
        {ISIN_upper: {internal_key: value, ...}}
    """
    if not all([isins, field_keys, token]):
        return {}

    import datetime
    today = datetime.date.today().isoformat()

    # Datapoint-IDs für angeforderte Felder
    dp_ids = [_DP_ID_MAP[k] for k in field_keys if k in _DP_ID_MAP]
    rev_map = {v: k for k, v in _DP_ID_MAP.items() if k in field_keys}
    if not dp_ids:
        raise RuntimeError(
            "Keine MCP-Datapoint-IDs für die gewählten Felder. "
            "Bitte andere Attribute wählen."
        )

    results: dict[str, dict] = {}

    for i in range(0, len(isins), _BATCH_SIZE):
        batch = isins[i : i + _BATCH_SIZE]
        try:
            sess = _MCPSession(token)
            sess.initialize()

            # Schritt 1: ISINs → Morningstar investment_ids
            id_result = sess.call_tool(
                "morningstar-id-lookup-tool",
                {"investment_identifiers": batch},
            )
            investments = id_result.get("investments", {})

            isin_to_msid: dict[str, str] = {}
            for isin in batch:
                hits = investments.get(isin.upper()) or investments.get(isin) or []
                if not hits:
                    logger.info(f"Morningstar: keine ID für {isin}")
                    continue
                # F...-IDs sind Fonds-Level (besser für Stammdaten), 0P...-IDs sind Anteilsklassen
                preferred = next((h for h in hits if h["morningstar_id"].startswith("F")), hits[0])
                isin_to_msid[isin.upper()] = preferred["morningstar_id"]

            if not isin_to_msid:
                continue

            # Schritt 2: Daten abrufen
            ms_ids = list(set(isin_to_msid.values()))
            data_result = sess.call_tool(
                "morningstar-data-tool",
                {"investment_ids": ms_ids, "datapoint_ids": dp_ids},
            )
            result_by_msid = data_result.get("result", {})

            for isin_upper, ms_id in isin_to_msid.items():
                ms_entry = result_by_msid.get(ms_id, {})
                values   = ms_entry.get("values", [])
                parsed   = {
                    rev_map[v["datapointId"]]: str(v.get("value") or "")
                    for v in values
                    if v.get("datapointId") in rev_map
                }
                if parsed:
                    parsed["ms_fetch_date"] = today
                    results[isin_upper] = parsed
                    logger.info(f"Morningstar: {isin_upper} → {len(parsed)} Felder")

        except Exception as exc:
            logger.warning(f"Morningstar Batch-Fehler {batch}: {exc}")

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
