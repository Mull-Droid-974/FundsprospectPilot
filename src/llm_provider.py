"""
LLM-Abstraktionsschicht: Anthropic Claude + Google Gemini + OpenRouter.

Alle Aufrufe laufen über classify() und validate_key() — der Rest des
Tools muss nicht wissen, welcher Anbieter gerade aktiv ist.
"""

import os
from typing import Optional

from dotenv import load_dotenv

from utils import logger

load_dotenv()

# ─── Unterstützte Modelle pro Anbieter ────────────────────────────
OPENROUTER_MODELS = [
    "qwen/qwen-2.5-72b-instruct",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-large",
    "deepseek/deepseek-chat",
    "google/gemma-3-27b-it",
]

MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-3-flash-preview",
        "gemini-3-pro-preview",
    ],
    "openrouter": OPENROUTER_MODELS,
}

# Standard-Modelle (Batch / Einzel-PDF)
DEFAULT_BATCH_MODELS = {
    "anthropic":  os.getenv("CLAUDE_BATCH_MODEL",       "claude-haiku-4-5-20251001"),
    "gemini":     os.getenv("GEMINI_BATCH_MODEL",        "gemini-2.5-flash"),
    "openrouter": os.getenv("OPENROUTER_BATCH_MODEL",    "qwen/qwen-2.5-72b-instruct"),
}
DEFAULT_SINGLE_MODELS = {
    "anthropic":  os.getenv("CLAUDE_SINGLE_MODEL",       "claude-sonnet-4-6"),
    "gemini":     os.getenv("GEMINI_SINGLE_MODEL",        "gemini-2.5-pro"),
    "openrouter": os.getenv("OPENROUTER_SINGLE_MODEL",   "qwen/qwen-2.5-72b-instruct"),
}

# Aktiver Anbieter
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")


def classify(
    text: str,
    isin: str = "",
    fund_name: str = "",
    additional_context: str = "",
    api_key: str = "",
    model: str = "",
    provider: str = "",
) -> dict:
    """
    Klassifiziert einen Fondsprospekt-Text mit dem konfigurierten LLM-Anbieter.

    Args:
        text:               Extrahierter PDF-Text
        isin:               ISIN der Anteilsklasse
        fund_name:          Name des Fonds
        additional_context: Ergänzende Informationen (z.B. Web-Suche)
        api_key:            API-Key des Anbieters (leer → aus .env)
        model:              Modellname (leer → Batch-Standard des Anbieters)
        provider:           "anthropic" | "gemini" | "openrouter" (leer → DEFAULT_PROVIDER)

    Returns:
        Dict mit segmentierung, fondstyp, anlegertyp, kundentyp, begruendung, konfidenz
    """
    p = (provider or DEFAULT_PROVIDER).lower()
    resolved_model = model or DEFAULT_BATCH_MODELS.get(p, "")

    logger.info(f"LLM-Anbieter: {p} | Modell: {resolved_model}")

    if p == "anthropic":
        from claude_classifier import classify_prospectus
        key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        return classify_prospectus(
            text, isin=isin, fund_name=fund_name,
            additional_context=additional_context,
            api_key=key, model=resolved_model,
        )

    elif p == "gemini":
        from gemini_classifier import classify_with_gemini
        key = api_key or os.getenv("GOOGLE_API_KEY", "")
        return classify_with_gemini(
            text, isin=isin, fund_name=fund_name,
            additional_context=additional_context,
            api_key=key, model=resolved_model,
        )

    elif p == "openrouter":
        from openrouter_classifier import classify_with_openrouter
        key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        return classify_with_openrouter(
            text, isin=isin, fund_name=fund_name,
            additional_context=additional_context,
            api_key=key, model=resolved_model,
        )

    else:
        raise ValueError(f"Unbekannter LLM-Anbieter: '{p}'. Erlaubt: anthropic, gemini, openrouter")


def validate_key(api_key: str, provider: str) -> tuple[bool, str]:
    """
    Testet einen API-Key mit einem minimalen Aufruf.

    Returns:
        (True, "Modellname")        bei Erfolg
        (False, "Fehlermeldung")    bei Fehler
    """
    p = provider.lower()

    if p == "anthropic":
        from claude_classifier import validate_api_key as _val
        ok, msg = _val(api_key)
        return ok, msg

    elif p == "gemini":
        from gemini_classifier import validate_gemini_key
        return validate_gemini_key(api_key)

    elif p == "openrouter":
        from openrouter_classifier import validate_openrouter_key
        return validate_openrouter_key(api_key)

    else:
        return False, f"Unbekannter Anbieter: {provider}"


def get_models(provider: str) -> list[str]:
    """Gibt die verfügbaren Modelle für einen Anbieter zurück."""
    return MODELS.get(provider.lower(), [])


def get_default_batch_model(provider: str) -> str:
    return DEFAULT_BATCH_MODELS.get(provider.lower(), "")


def get_default_single_model(provider: str) -> str:
    return DEFAULT_SINGLE_MODELS.get(provider.lower(), "")
