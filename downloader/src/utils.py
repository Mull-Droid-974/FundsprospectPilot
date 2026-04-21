"""Hilfsfunktionen: Logging, PDF-Nummerierung, Pfadverwaltung."""

import logging
import os
import re
from pathlib import Path


def setup_logging(log_file: str = "data/output/errors.log") -> logging.Logger:
    """Richtet Logging ein (Konsole + Datei)."""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger("fundsprospect")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        # Konsolen-Handler (UTF-8 für Emojis auf Windows)
        import sys
        ch = logging.StreamHandler(stream=open(
            sys.stdout.fileno(), mode="w", encoding="utf-8",
            buffering=1, closefd=False
        ) if hasattr(sys.stdout, "fileno") else sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(ch)

        # Datei-Handler
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    return logger


logger = setup_logging()


def get_next_pdf_number(pdf_folder: str) -> int:
    """Ermittelt die nächste freie 5-stellige Nummer (ab 11111)."""
    folder = Path(pdf_folder)
    folder.mkdir(parents=True, exist_ok=True)

    existing = []
    for f in folder.iterdir():
        match = re.match(r'^(\d{5})_', f.name)
        if match:
            existing.append(int(match.group(1)))

    return max(existing, default=11110) + 1


def sanitize_filename(name: str, max_length: int = 60) -> str:
    """Bereinigt einen String für die Verwendung als Dateiname."""
    # Ungültige Zeichen entfernen
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Mehrfache Leerzeichen/Unterstriche zusammenfassen
    name = re.sub(r'[\s_]+', '_', name).strip('_')
    return name[:max_length] if name else "unknown"


def build_pdf_filename(number: int, fund_name: str) -> str:
    """Erstellt den PDF-Dateinamen: z.B. '11111_FundName.pdf'."""
    safe_name = sanitize_filename(fund_name)
    return f"{number:05d}_{safe_name}.pdf"


def truncate_text(text: str, max_chars: int = 150_000) -> str:
    """Kürzt Text auf max_chars Zeichen, bevorzugt an Absatzgrenzen."""
    if len(text) <= max_chars:
        return text

    # Versuche, an einem Absatz zu kürzen
    truncated = text[:max_chars]
    last_break = truncated.rfind('\n\n')
    if last_break > max_chars * 0.8:
        return truncated[:last_break]
    return truncated


def extract_relevant_sections(text: str) -> str:
    """
    Extrahiert die für die Klassifizierung relevanten Abschnitte aus dem PDF-Text.
    Sucht nach Schlüsselwörtern zu Anlegertyp, Zielmarkt, Anteilsklassen etc.
    """
    keywords = [
        "anleger", "investor", "zielmarkt", "target market",
        "anteilsklasse", "share class", "tranche",
        "institutional", "institutionell", "retail", "privat",
        "professionell", "professional", "qualified",
        "qualifiziert", "verkaufsbeschränkung", "restriction",
        "mifid", "priips", "ucits", "aif",
        "mindestanlage", "minimum investment",
        "vertrieb", "distribution", "placement",
    ]

    lines = text.split('\n')
    relevant_lines = []
    context_window = 5  # Zeilen vor und nach einem Treffer

    hit_positions = set()
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(kw in line_lower for kw in keywords):
            for j in range(max(0, i - context_window), min(len(lines), i + context_window + 1)):
                hit_positions.add(j)

    for i in sorted(hit_positions):
        relevant_lines.append(lines[i])

    result = '\n'.join(relevant_lines)

    # Falls zu wenig gefunden, den Anfang des Dokuments verwenden
    if len(result) < 500:
        result = text[:10000]

    return truncate_text(result, max_chars=80_000)


# ─── Gezielte Abschnitts-Extraktion (für LLM-Fallback mit reduziertem Kontext) ──

_TARGETED_SECTION_RE = re.compile(
    r'^(?:'
    r'Zielmarkt'
    r'|Zulässige\s+Anleger'
    r'|Eligible\s+Investors?'
    r'|Anlegerprofil'
    r'|Anlegerkreis'
    r'|Intended\s+Investors?'
    r'|Vertriebsbeschränkungen?'
    r'|Distribution\s+Restrictions?'
    r'|Zulässige\s+Anteilsinhaber'
    r'|Target\s+(?:Market|Investors?)'
    r'|Anteilsklassen?'
    r'|Share\s+Classes?'
    r')[\s:]*$',
    re.IGNORECASE | re.MULTILINE,
)

_MAX_TARGETED = 8_000
_HEAD_CHARS = 1_500
_MAX_PER_SECTION = 2_500


def extract_targeted_sections(text: str) -> str:
    """
    Findet vollständige Abschnittsblöcke zu regulatorisch relevanten Titeln.
    Gibt max. 8.000 Zeichen zurück (statt 80.000 bei extract_relevant_sections).
    Wird im LLM-Fallback verwendet um Token-Kosten zu reduzieren.
    """
    parts = []
    total = 0

    # Immer den Dokumentanfang einschliessen (Deckblatt, Prospektart)
    head = text[:_HEAD_CHARS]
    parts.append(f"[DOKUMENTANFANG]\n{head}")
    total += len(head)

    lines = text.split('\n')
    i = 0
    while i < len(lines) and total < _MAX_TARGETED:
        if _TARGETED_SECTION_RE.match(lines[i].strip()):
            section_lines = [lines[i]]
            chars = len(lines[i])
            j = i + 1
            while j < len(lines) and chars < _MAX_PER_SECTION:
                next_stripped = lines[j].strip()
                if _TARGETED_SECTION_RE.match(next_stripped) and j > i + 1:
                    break
                section_lines.append(lines[j])
                chars += len(lines[j]) + 1
                j += 1
            section_text = '\n'.join(section_lines)
            remaining = _MAX_TARGETED - total
            chunk = section_text[:remaining]
            parts.append(f"\n[ABSCHNITT: {lines[i].strip()[:40]}]\n{chunk}")
            total += len(chunk)
            i = j
        else:
            i += 1

    result = '\n'.join(parts)

    # Fallback: zu wenig gefunden → bestehende Funktion mit kleinerem Budget
    if total < 3_000:
        return extract_relevant_sections(text)[:_MAX_TARGETED]

    return result[:_MAX_TARGETED]
