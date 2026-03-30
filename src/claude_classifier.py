"""Claude API Klassifizierung von Fondsprospekten."""

import json
import os
from typing import Optional

import anthropic
from dotenv import load_dotenv

from utils import logger

load_dotenv()

# Modell-Auswahl:
# - claude-haiku-4-5-20251001  → für Batch (günstig, schnell)
# - claude-sonnet-4-6          → für Einzel-PDF (bessere Qualität)
# - claude-opus-4-6            → für schwierige Fälle
DEFAULT_BATCH_MODEL = os.getenv("CLAUDE_BATCH_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_SINGLE_MODEL = os.getenv("CLAUDE_SINGLE_MODEL", "claude-sonnet-4-6")

# Klassifizierungsergebnis-Struktur
EMPTY_RESULT = {
    "segmentierung": "unklar",
    "fondstyp": "",
    "anlegertyp": "",
    "kundentyp": "",
    "begruendung": "",
    "konfidenz": "niedrig",
}

SYSTEM_PROMPT = """Du bist ein Experte für Investmentfonds-Regulierung in der Schweiz und Europa.
Deine Aufgabe ist es, aus Verkaufsprospekten (Fondsprospekten) die Zielgruppe der Anteilsklassen zu bestimmen.

Analysiere den Text und extrahiere folgende Informationen:

1. **segmentierung**: Ist die Anteilsklasse für institutionelle oder Retail-Anleger?
   - "institutional" = für professionelle/qualifizierte/institutionelle Anleger
   - "retail" = für Privatanleger/alle Anleger
   - "unklar" = kann nicht eindeutig bestimmt werden

2. **fondstyp**: Art des Fonds (z.B. "UCITS", "AIF", "Hedgefonds", "Immobilienfonds", "ETF", "SICAV", etc.)

3. **anlegertyp**: Zulässige Anleger laut Prospekt (z.B. "Qualifizierte Anleger", "Professionelle Anleger",
   "Alle Anleger", "Institutionelle Anleger", "Semi-professionelle Anleger")

4. **kundentyp**: MiFID II Klassifizierung (z.B. "MiFID Retail", "MiFID Professional",
   "Eligible Counterparty", "Geeignete Gegenpartei")

5. **begruendung**: Kurze Begründung der Segmentierungsentscheidung (1-2 Sätze)

6. **konfidenz**: Wie sicher bist du dir? ("hoch", "mittel", "niedrig")

Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. Kein Text davor oder danach.
Beispiel:
{
  "segmentierung": "institutional",
  "fondstyp": "UCITS",
  "anlegertyp": "Professionelle Anleger",
  "kundentyp": "MiFID Professional",
  "begruendung": "Der Prospekt schränkt den Vertrieb explizit auf professionelle Anleger gemäss MiFID II ein.",
  "konfidenz": "hoch"
}"""


def classify_prospectus(
    text: str,
    isin: str = "",
    fund_name: str = "",
    additional_context: str = "",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    Klassifiziert einen Fondsprospekt-Text via Claude API.

    Args:
        text: Extrahierter PDF-Text (oder relevanter Ausschnitt)
        isin: ISIN der Anteilsklasse (für Kontext)
        fund_name: Name des Fonds (für Kontext)
        additional_context: Zusätzliche Informationen (z.B. aus Web-Suche)
        api_key: Anthropic API-Key (optional, sonst aus .env)
        model: Claude-Modell (optional; Standard: haiku für Batch, sonnet für Einzel)

    Returns:
        Dict mit segmentierung, fondstyp, anlegertyp, kundentyp, begruendung, konfidenz
    """
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("Kein ANTHROPIC_API_KEY gefunden. Bitte .env Datei prüfen.")

    selected_model = model or DEFAULT_BATCH_MODEL
    client = anthropic.Anthropic(api_key=key)

    # Prompt aufbauen
    user_content = []
    if isin or fund_name:
        user_content.append(f"**ISIN:** {isin}\n**Fonds:** {fund_name}\n\n")

    user_content.append("**Auszug aus dem Verkaufsprospekt:**\n\n")
    user_content.append(text[:80_000])  # Sicherheits-Limit

    if additional_context:
        user_content.append(f"\n\n**Zusätzliche Informationen aus der Web-Recherche:**\n{additional_context[:5000]}")

    user_message = "".join(user_content)

    logger.info(f"Sende {len(user_message):,} Zeichen an Claude API (ISIN: {isin}, Modell: {selected_model})")

    try:
        response = client.messages.create(
            model=selected_model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        # Text-Block aus der Antwort extrahieren
        result_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                result_text += block.text

        logger.info(f"Claude Antwort erhalten ({len(result_text)} Zeichen)")

        # JSON parsen
        return _parse_result(result_text, isin)

    except anthropic.AuthenticationError:
        raise ValueError("Ungültiger API-Key. Bitte .env Datei prüfen.")
    except anthropic.RateLimitError:
        raise RuntimeError("API Rate Limit erreicht. Bitte kurz warten und erneut versuchen.")
    except Exception as e:
        logger.error(f"Claude API Fehler für ISIN {isin}: {e}")
        raise


def _parse_result(text: str, isin: str = "") -> dict:
    """Parst die JSON-Antwort von Claude."""
    # JSON-Block aus der Antwort extrahieren
    text = text.strip()

    # Manchmal ist JSON in Markdown-Blöcken eingebettet
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # Erstes { bis letztes } extrahieren
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    try:
        result = json.loads(text)

        # Fehlende Felder mit Defaults füllen
        for key, default in EMPTY_RESULT.items():
            if key not in result:
                result[key] = default

        # Segmentierung normalisieren
        seg = result.get("segmentierung", "unklar").lower().strip()
        if "institut" in seg:
            result["segmentierung"] = "institutional"
        elif "retail" in seg or "privat" in seg:
            result["segmentierung"] = "retail"
        else:
            result["segmentierung"] = "unklar"

        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON-Parsing fehlgeschlagen für ISIN {isin}: {e}\nAntwort: {text[:500]}")
        result = EMPTY_RESULT.copy()
        result["begruendung"] = f"JSON-Parsing fehlgeschlagen: {str(e)[:100]}"
        return result


def validate_api_key(api_key: str) -> tuple[bool, str]:
    """
    Prüft ob der Anthropic API-Key gültig ist.

    Returns:
        (True, "claude-haiku-4-5-20251001")  bei Erfolg
        (False, "Fehlermeldung")             bei Fehler
    """
    test_model = "claude-haiku-4-5-20251001"
    try:
        client = anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model=test_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "test"}],
        )
        return True, test_model
    except anthropic.AuthenticationError:
        return False, "Ungültiger API-Key"
    except anthropic.PermissionDeniedError:
        return False, "Zugriff verweigert"
    except Exception as e:
        return False, f"Fehler: {str(e)[:120]}"
