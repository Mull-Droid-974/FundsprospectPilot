"""
OpenRouter Klassifizierung von Fondsprospekten.

OpenRouter ist ein OpenAI-kompatibler API-Gateway mit Zugang zu 40+ Modellen.
Nutzt das OpenAI-SDK mit benutzerdefinierter base_url.
"""

import json
from utils import logger

EMPTY_RESULT = {
    "segmentierung": "unklar",
    "fondstyp": "",
    "anlegertyp": "",
    "kundentyp": "",
    "begruendung": "",
    "konfidenz": "niedrig",
}

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _openrouter_client(api_key: str):
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai ist nicht installiert. "
            "Bitte 'pip install openai' ausführen."
        )
    return OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL)


def classify_with_openrouter(
    text: str,
    isin: str = "",
    fund_name: str = "",
    additional_context: str = "",
    api_key: str = "",
    model: str = "qwen/qwen-2.5-72b-instruct",
    prompt: str = "",
) -> dict:
    """
    Klassifiziert einen Fondsprospekt-Text via OpenRouter API (OpenAI-SDK).
    """
    if not api_key:
        raise ValueError("Kein OPENROUTER_API_KEY vorhanden. Bitte im Admin-Bereich eintragen.")

    client = _openrouter_client(api_key)

    parts = []
    if isin or fund_name:
        parts.append(f"**ISIN:** {isin}\n**Fonds:** {fund_name}\n\n")
    parts.append("**Auszug aus dem Verkaufsprospekt:**\n\n")
    parts.append(text[:80_000])
    if additional_context:
        parts.append(f"\n\n**Zusätzliche Informationen:**\n{additional_context[:5000]}")

    user_message = "".join(parts)
    logger.info(f"Sende {len(user_message):,} Zeichen an OpenRouter API (ISIN: {isin}, Modell: {model})")

    messages = [{"role": "user", "content": user_message}]
    if prompt:
        messages = [{"role": "system", "content": prompt}] + messages

    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            messages=messages,
        )
        raw = (response.choices[0].message.content or "") if response.choices else ""
        logger.info(f"OpenRouter Antwort erhalten ({len(raw)} Zeichen)")
        return _parse_result(raw, isin)

    except Exception as e:
        err = str(e)
        if "401" in err or "invalid_api_key" in err.lower() or "authentication" in err.lower():
            raise ValueError("Ungültiger OpenRouter API-Key.")
        if "429" in err or "rate_limit" in err.lower() or "quota" in err.lower():
            raise RuntimeError("OpenRouter Rate Limit / Quota erreicht.")
        logger.error(f"OpenRouter API Fehler für ISIN {isin}: {e}")
        raise


def validate_openrouter_key(api_key: str) -> tuple[bool, str]:
    """
    Testet den OpenRouter API-Key mit einem minimalen Aufruf.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return False, "openai nicht installiert (pip install openai)"

    test_model = "qwen/qwen-2.5-72b-instruct"
    try:
        client = OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL)
        response = client.chat.completions.create(
            model=test_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "Antworte nur mit: ok"}],
        )
        if response.choices:
            return True, test_model
        return False, "Leere Antwort vom Modell"
    except Exception as e:
        err = str(e)
        if "401" in err or "invalid_api_key" in err.lower() or "authentication" in err.lower():
            return False, "Ungültiger API-Key"
        if "403" in err or "permission" in err.lower():
            return False, "Zugriff verweigert"
        if "429" in err or "rate_limit" in err.lower():
            return True, f"{test_model} (Key gültig, Rate Limit erreicht)"
        return False, f"Fehler: {err[:150]}"


def _parse_result(text: str, isin: str = "") -> dict:
    """Parst die JSON-Antwort von OpenRouter."""
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
            logger.warning(f"OpenRouter JSON-Antwort für ISIN {isin} automatisch repariert.")
            return _normalize(repaired)
    except Exception:
        pass

    logger.error(f"OpenRouter JSON-Parsing fehlgeschlagen für ISIN {isin}\nAntwort: {text[:500]}")
    result = EMPTY_RESULT.copy()
    result["begruendung"] = "JSON-Parsing fehlgeschlagen"
    return result
