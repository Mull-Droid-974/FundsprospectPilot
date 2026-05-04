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
    "gemini-2.5-flash-preview-05-20": 0.15,
    "gemini-2.5-pro-preview-05-06":   1.25,
}


def classify_with_gemini(
    text: str,
    isin: str = "",
    fund_name: str = "",
    additional_context: str = "",
    api_key: str = "",
    model: str = "gemini-2.5-flash-preview-05-20",
) -> dict:
    """
    Klassifiziert einen Fondsprospekt-Text via Google Gemini API.

    Returns:
        Dict mit segmentierung, fondstyp, anlegertyp, kundentyp, begruendung, konfidenz
    """
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai ist nicht installiert. "
            "Bitte 'pip install google-generativeai' ausführen."
        )

    if not api_key:
        raise ValueError("Kein GOOGLE_API_KEY vorhanden. Bitte im Admin-Bereich eintragen.")

    genai.configure(api_key=api_key)

    # Prompt aufbauen
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
        client = genai.GenerativeModel(
            model_name=model,
            system_instruction=SYSTEM_PROMPT,
        )
        response = client.generate_content(user_message)
        result_text = response.text or ""
        logger.info(f"Gemini Antwort erhalten ({len(result_text)} Zeichen)")
        return _parse_result(result_text, isin)

    except Exception as e:
        err = str(e)
        if "API_KEY_INVALID" in err or "INVALID_ARGUMENT" in err:
            raise ValueError("Ungültiger Google API-Key.")
        if "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
            raise RuntimeError("Gemini API Rate Limit / Quota erreicht.")
        logger.error(f"Gemini API Fehler für ISIN {isin}: {e}")
        raise


def validate_gemini_key(api_key: str) -> tuple[bool, str]:
    """
    Testet den Google API-Key mit einem minimalen Aufruf.

    Returns:
        (True, "gemini-2.5-flash-preview-05-20") bei Erfolg
        (False, "Fehlermeldung")                 bei Fehler
    """
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai nicht installiert (pip install google-generativeai)"

    test_model = "gemini-2.5-flash-preview-05-20"
    try:
        genai.configure(api_key=api_key)
        client = genai.GenerativeModel(model_name=test_model)
        response = client.generate_content("Antworte nur mit: ok")
        if response.text:
            return True, test_model
        return False, "Leere Antwort vom Modell"
    except Exception as e:
        err = str(e)
        if "API_KEY_INVALID" in err or "INVALID_ARGUMENT" in err:
            return False, "Ungültiger API-Key"
        if "PERMISSION_DENIED" in err:
            return False, "Zugriff verweigert (API nicht aktiviert?)"
        return False, f"Fehler: {err[:120]}"


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

    try:
        result = json.loads(text)
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

    except json.JSONDecodeError as e:
        logger.error(f"Gemini JSON-Parsing fehlgeschlagen für ISIN {isin}: {e}\nAntwort: {text[:500]}")
        result = EMPTY_RESULT.copy()
        result["begruendung"] = f"JSON-Parsing fehlgeschlagen: {str(e)[:100]}"
        return result
