"""
Google Gemini Klassifizierung von Fondsprospekten.

Nutzt dieselbe Prompt-Logik wie claude_classifier.py.
Unterstützte Modelle: gemini-2.5-flash, gemini-2.5-pro
"""

import json
import re
from typing import Optional

from utils import logger

# Selber System-Prompt wie für Anthropic (provider-unabhängig)
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

EMPTY_RESULT = {
    "segmentierung": "unklar",
    "fondstyp": "",
    "anlegertyp": "",
    "kundentyp": "",
    "begruendung": "",
    "konfidenz": "niedrig",
}

# Kosten-Heuristik (USD pro 1M Tokens, Input)
COST_PER_MTOK: dict[str, float] = {
    "gemini-2.5-flash":      0.15,
    "gemini-2.5-flash-lite": 0.10,
    "gemini-2.5-pro":        1.25,
    "gemini-2.0-flash":      0.10,
    "gemini-3-flash-preview": 0.15,
    "gemini-3-pro-preview":  1.25,
}


def _gemini_client(api_key: str):
    try:
        from google import genai
    except ImportError:
        raise ImportError(
            "google-genai ist nicht installiert. "
            "Bitte 'pip install google-genai' ausführen."
        )
    return genai.Client(api_key=api_key)


def classify_with_gemini(
    text: str,
    isin: str = "",
    fund_name: str = "",
    additional_context: str = "",
    api_key: str = "",
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    Klassifiziert einen Fondsprospekt-Text via Google Gemini API (google-genai SDK).
    """
    if not api_key:
        raise ValueError("Kein GOOGLE_API_KEY vorhanden. Bitte im Admin-Bereich eintragen.")

    from google.genai import types

    client = _gemini_client(api_key)

    parts = []
    if isin or fund_name:
        parts.append(f"**ISIN:** {isin}\n**Fonds:** {fund_name}\n\n")
    parts.append("**Auszug aus dem Verkaufsprospekt:**\n\n")
    parts.append(text[:80_000])
    if additional_context:
        parts.append(f"\n\n**Zusätzliche Informationen:**\n{additional_context[:5000]}")

    user_message = "".join(parts)
    logger.info(f"Sende {len(user_message):,} Zeichen an Gemini API (ISIN: {isin}, Modell: {model})")

    try:
        response = client.models.generate_content(
            model=model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=8192,
            ),
        )
        result_text = response.text or ""
        logger.info(f"Gemini Antwort erhalten ({len(result_text)} Zeichen)")
        return _parse_result(result_text, isin)

    except Exception as e:
        err = str(e)
        if "UNAUTHENTICATED" in err or "API_KEY_INVALID" in err or "401" in err:
            raise ValueError("Ungültiger Google API-Key.")
        if "RESOURCE_EXHAUSTED" in err or "quota" in err.lower() or "429" in err:
            raise RuntimeError("Gemini API Rate Limit / Quota erreicht.")
        logger.error(f"Gemini API Fehler für ISIN {isin}: {e}")
        raise


def validate_gemini_key(api_key: str) -> tuple[bool, str]:
    """
    Testet den Google API-Key mit einem minimalen Aufruf (google-genai SDK).
    """
    try:
        from google.genai import types
    except ImportError:
        return False, "google-genai nicht installiert (pip install google-genai)"

    test_model = "gemini-2.5-flash"
    try:
        client = _gemini_client(api_key)
        response = client.models.generate_content(
            model=test_model,
            contents="Antworte nur mit: ok",
            config=types.GenerateContentConfig(max_output_tokens=10),
        )
        if response.text:
            return True, test_model
        return False, "Leere Antwort vom Modell"
    except Exception as e:
        err = str(e)
        if "UNAUTHENTICATED" in err or "API_KEY_INVALID" in err or "401" in err:
            return False, "Ungültiger API-Key"
        if "PERMISSION_DENIED" in err or "403" in err:
            return False, "Zugriff verweigert (API nicht aktiviert?)"
        if "RESOURCE_EXHAUSTED" in err or "429" in err:
            return True, f"{test_model} (Key gültig, Quota erreicht)"
        if "UNAVAILABLE" in err or "503" in err:
            return True, f"{test_model} (Key gültig, Modell kurz überlastet)"
        return False, f"Fehler: {err[:150]}"


def _parse_result(text: str, isin: str = "") -> dict:
    """Parst die JSON-Antwort von Gemini."""
    text = text.strip()

    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    def _normalize(result: dict) -> dict:
        for key, default in EMPTY_RESULT.items():
            if key not in result:
                result[key] = default
        seg = result.get("segmentierung", "unklar").lower().strip()
        if "institut" in seg:
            result["segmentierung"] = "institutional"
        elif "retail" in seg or "privat" in seg:
            result["segmentierung"] = "retail"
        else:
            result["segmentierung"] = "unklar"
        return result

    try:
        return _normalize(json.loads(text))
    except json.JSONDecodeError:
        pass

    try:
        from json_repair import repair_json
        repaired = repair_json(text, return_objects=True)
        if isinstance(repaired, dict):
            logger.warning(f"Gemini JSON-Antwort für ISIN {isin} automatisch repariert.")
            return _normalize(repaired)
    except Exception:
        pass

    logger.error(f"Gemini JSON-Parsing fehlgeschlagen für ISIN {isin}\nAntwort: {text[:500]}")
    result = EMPTY_RESULT.copy()
    result["begruendung"] = "JSON-Parsing fehlgeschlagen"
    return result
