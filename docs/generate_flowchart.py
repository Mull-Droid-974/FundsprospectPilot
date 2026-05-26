"""Generiert das FundProspektPilot-Flowchart als PNG mit Pillow."""

from PIL import Image, ImageDraw, ImageFont
import sys, os

# ── Farben (Catppuccin Mocha) ─────────────────────────────────────────────────
BG        = (30, 30, 46)       # #1e1e2e
PANEL     = (42, 42, 62)       # #2a2a3e
CARD      = (49, 49, 69)       # #313145
BORDER    = (69, 71, 90)       # #45475a
BLUE      = (137, 180, 250)    # #89b4fa
GREEN     = (166, 227, 161)    # #a6e3a1
RED       = (243, 139, 168)    # #f38ba8
YELLOW    = (249, 226, 175)    # #f9e2af
LAVENDER  = (180, 190, 254)    # #b4befe
PURPLE    = (203, 166, 247)    # #cba6f7
CYAN      = (137, 220, 235)    # #89dceb
TEXT      = (205, 214, 244)    # #cdd6f4
MUTED     = (127, 132, 156)    # #7f849c


def load_font(size):
    """Lädt Segoe UI oder Fallback."""
    candidates = [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def load_font_bold(size):
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Zeichen-Helfer ────────────────────────────────────────────────────────────

def draw_rounded_rect(draw, x1, y1, x2, y2, r, fill, outline, width=2):
    draw.rounded_rectangle([x1, y1, x2, y2], radius=r,
                           fill=fill, outline=outline, width=width)


def draw_diamond(draw, cx, cy, w, h, fill, outline, width=2):
    pts = [(cx, cy - h//2), (cx + w//2, cy), (cx, cy + h//2), (cx - w//2, cy)]
    draw.polygon(pts, fill=fill, outline=outline)
    # Redraw outline with correct width
    for i in range(len(pts)):
        a, b = pts[i], pts[(i+1) % len(pts)]
        draw.line([a, b], fill=outline, width=width)


def draw_arrow(draw, x1, y1, x2, y2, color=MUTED, width=2, label="", font=None):
    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    # Arrowhead
    import math
    angle = math.atan2(y2 - y1, x2 - x1)
    head = 10
    ax1 = x2 - head * math.cos(angle - 0.4)
    ay1 = y2 - head * math.sin(angle - 0.4)
    ax2 = x2 - head * math.cos(angle + 0.4)
    ay2 = y2 - head * math.sin(angle + 0.4)
    draw.polygon([(x2, y2), (int(ax1), int(ay1)), (int(ax2), int(ay2))], fill=color)
    if label and font:
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        draw.text((mx + 4, my - 14), label, fill=MUTED, font=font)


def centered_text(draw, text, cx, cy, font, color=TEXT, line_height=None):
    lines = text.split("\n")
    if line_height is None:
        bbox = draw.textbbox((0, 0), "Ag", font=font)
        line_height = bbox[3] - bbox[1] + 4
    total_h = len(lines) * line_height
    y = cy - total_h // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        draw.text((cx - w // 2, y), line, fill=color, font=font)
        y += line_height


# ── Box-Typen ─────────────────────────────────────────────────────────────────

def box(draw, cx, cy, w, h, label, color=CARD, border=BORDER, text_color=TEXT, font=None, bold_font=None):
    x1, y1, x2, y2 = cx - w//2, cy - h//2, cx + w//2, cy + h//2
    draw_rounded_rect(draw, x1, y1, x2, y2, 10, color, border, 2)
    centered_text(draw, label, cx, cy, bold_font or font, text_color)
    return (x1, y1, x2, y2)


def diamond(draw, cx, cy, w, h, label, color=CARD, border=BLUE, text_color=BLUE, font=None):
    draw_diamond(draw, cx, cy, w, h, color, border, 2)
    centered_text(draw, label, cx, cy, font, text_color)


def terminal(draw, cx, cy, w, h, label, color=PANEL, border=MUTED, text_color=MUTED, font=None):
    x1, y1, x2, y2 = cx - w//2, cy - h//2, cx + w//2, cy + h//2
    draw.ellipse([x1, y1, x2, y2], fill=color, outline=border, width=2)
    centered_text(draw, label, cx, cy, font, text_color)


# ═════════════════════════════════════════════════════════════════════════════
# HAUPT-FLOWCHART
# ═════════════════════════════════════════════════════════════════════════════

def generate(out_path: str):
    W, H = 1400, 4800
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    fn   = load_font(15)
    fnb  = load_font_bold(16)
    fns  = load_font(13)
    fntitle = load_font_bold(28)
    fnsub   = load_font(17)

    # Titel
    title = "FundProspektPilot — System-Flowchart"
    bbox = draw.textbbox((0, 0), title, font=fntitle)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, 28), title, fill=BLUE, font=fntitle)
    sub = "Alle Regeln und Abläufe"
    bbox2 = draw.textbbox((0, 0), sub, font=fnsub)
    draw.text(((W - bbox2[2]) // 2, 68), sub, fill=MUTED, font=fnsub)

    cx = W // 2   # Haupt-Mittellinie

    # ── SECTION LABELS ──────────────────────────────────────────────────────
    def section_label(y, text, color=MUTED):
        draw.text((40, y), text, fill=color, font=load_font_bold(13))
        draw.line([(40, y + 20), (W - 40, y + 20)], fill=BORDER, width=1)

    # ── START ────────────────────────────────────────────────────────────────
    y = 130
    terminal(draw, cx, y, 200, 44, "Start: ISIN-Liste", PANEL, BLUE, BLUE, fnb)

    draw_arrow(draw, cx, y+22, cx, y+70, MUTED, 2)

    # ── IMPORT ───────────────────────────────────────────────────────────────
    y += 92
    box(draw, cx, y, 300, 52, "Daten importieren\nExcel / CSV / manuell", CARD, BORDER, TEXT, fn, fnb)

    draw_arrow(draw, cx, y+26, cx, y+60, MUTED, 2)

    # ── DB ───────────────────────────────────────────────────────────────────
    y += 82
    box(draw, cx, y, 260, 52, "SQLite-Datenbank\nresults.db", PANEL, LAVENDER, LAVENDER, fn, fnb)

    draw_arrow(draw, cx, y+26, cx, y+60, MUTED, 2)

    # ═══════════════════════════════════════════════════════════
    # PHASE 1
    # ═══════════════════════════════════════════════════════════
    y += 82
    section_label(y - 10, "PHASE 1 — Metadaten-Beschaffung (fundinfo API, parallel)", BLUE)
    y += 28

    diamond(draw, cx, y, 280, 70, "Metadaten\nvorhanden?", CARD, BLUE, BLUE, fn)

    # JA-Zweig → rechts überspringen
    draw_arrow(draw, cx+140, y, cx+280, y, MUTED, 2, "Ja", fns)
    # NEIN-Zweig → runter
    draw_arrow(draw, cx, y+35, cx, y+90, MUTED, 2, "Nein", fns)

    # Metadaten-Box
    y_meta = y + 115
    box(draw, cx, y_meta, 380, 80,
        "Phase 1: fundinfo API\nsubfonds_id, subfonds_name, prospekt_url,\nTER, Anlegertyp, Anteilsklassen",
        CARD, BLUE, TEXT, fns, fnb)

    # Geschwister entdecken
    y_sibling = y_meta + 110
    box(draw, cx - 220, y_sibling, 280, 60,
        "Geschwister-ISINs\nentdecken & in DB anlegen",
        CARD, CYAN, CYAN, fns)

    draw_arrow(draw, cx - 110, y_meta + 40, cx - 110, y_sibling - 30, MUTED, 2)
    draw.line([(cx - 110, y_meta + 40), (cx - 220, y_meta + 40)], fill=MUTED, width=2)

    # Fehler Metadaten
    box(draw, cx + 220, y_sibling, 240, 60,
        "Fehler:\nkein Datensatz auf\nfundinfo", CARD, RED, RED, fns)

    draw_arrow(draw, cx + 110, y_meta + 40, cx + 110, y_sibling - 30, MUTED, 2)
    draw.line([(cx + 110, y_meta + 40), (cx + 220, y_meta + 40)], fill=MUTED, width=2)

    # Verbindung JA-Zweig zu unterhalb Phase-1
    y_after_p1 = y_sibling + 80
    draw_arrow(draw, cx, y_meta + 40, cx, y_after_p1, MUTED, 2)
    # JA-Bypass: horizontal bei y, dann runter zu y_after_p1
    draw.line([(cx + 280, y), (cx + 280, y_after_p1)], fill=MUTED, width=2)
    draw.line([(cx + 280, y_after_p1), (cx + 6, y_after_p1)], fill=MUTED, width=2)

    # ═══════════════════════════════════════════════════════════
    # PHASE 2 – DOWNLOAD
    # ═══════════════════════════════════════════════════════════
    y = y_after_p1 + 20
    section_label(y, "PHASE 2 — Prospekt-Download (nach Subfonds gruppiert, parallel)", GREEN)
    y += 28

    diamond(draw, cx, y, 240, 70, "PDF\nvorhanden?", CARD, GREEN, GREEN, fn)

    draw_arrow(draw, cx+120, y, cx+280, y, MUTED, 2, "Ja", fns)
    draw_arrow(draw, cx, y+35, cx, y+90, MUTED, 2, "Nein", fns)

    y_dl = y + 115
    box(draw, cx, y_dl, 420, 100,
        "Gruppierter Download (1 PDF pro Subfonds)\n"
        "A) Vorhandener Pfad → andere ISINs verknüpfen\n"
        "B) URL aus DB oder fundinfo ermitteln\n"
        "C) PDF herunterladen & alle ISINs verknüpfen",
        CARD, GREEN, TEXT, fns, fnb)

    # Fehler Download
    box(draw, cx + 260, y_dl, 220, 60,
        "Kein Prospekt\ngefunden", CARD, RED, RED, fns)
    draw_arrow(draw, cx + 210, y_dl, cx + 260, y_dl, MUTED, 2)

    y_after_p2 = y_dl + 120
    draw_arrow(draw, cx, y_dl + 50, cx, y_after_p2, MUTED, 2)
    # JA-Bypass
    draw.line([(cx + 280, y), (cx + 280, y_after_p2)], fill=MUTED, width=2)
    draw.line([(cx + 280, y_after_p2), (cx + 6, y_after_p2)], fill=MUTED, width=2)

    # ═══════════════════════════════════════════════════════════
    # PDF-EXTRAKTION
    # ═══════════════════════════════════════════════════════════
    y = y_after_p2 + 20
    section_label(y, "PDF-EXTRAKTION — pdfplumber + optionales OCR", YELLOW)
    y += 28

    box(draw, cx, y, 340, 52, "PDF öffnen (pdfplumber)", CARD, YELLOW, TEXT, fn, fnb)
    draw_arrow(draw, cx, y+26, cx, y+70, MUTED, 2)

    y += 90
    diamond(draw, cx, y, 240, 70, "> 150 Seiten?", CARD, YELLOW, YELLOW, fn)
    draw_arrow(draw, cx+120, y, cx+240, y, MUTED, 2, "Nein", fns)
    draw_arrow(draw, cx, y+35, cx, y+85, MUTED, 2, "Ja", fns)

    y_warn = y + 110
    box(draw, cx, y_warn, 320, 52,
        "Warnung: nur erste 150 Seiten\nverarbeitet", CARD, YELLOW, YELLOW, fns)
    draw_arrow(draw, cx, y_warn + 26, cx, y_warn + 70, MUTED, 2)

    y_extract = y_warn + 95
    box(draw, cx, y_extract, 380, 70,
        "Seiten extrahieren\nText vorhanden → sammeln\nBild-Seite → OCR-Liste",
        CARD, BORDER, TEXT, fns, fnb)

    # JA-Bypass von Nein-Zweig
    draw.line([(cx + 240, y), (cx + 240, y_extract)], fill=MUTED, width=2)
    draw.line([(cx + 240, y_extract), (cx + 190, y_extract)], fill=MUTED, width=2)

    draw_arrow(draw, cx, y_extract + 35, cx, y_extract + 80, MUTED, 2)

    y_ocr = y_extract + 100
    diamond(draw, cx, y_ocr, 280, 70, "pytesseract/\npdf2image vorhanden?", CARD, CYAN, CYAN, fn)
    draw_arrow(draw, cx + 140, y_ocr, cx + 260, y_ocr, MUTED, 2, "Nein", fns)
    draw_arrow(draw, cx, y_ocr + 35, cx, y_ocr + 85, MUTED, 2, "Ja", fns)

    box(draw, cx + 310, y_ocr, 180, 52, "Warnung:\nkein OCR", CARD, RED, RED, fns)

    y_ocr_do = y_ocr + 110
    box(draw, cx, y_ocr_do, 300, 52, "OCR auf Bild-Seiten\n(pytesseract)", CARD, CYAN, CYAN, fns)
    draw_arrow(draw, cx, y_ocr_do + 26, cx, y_ocr_do + 70, MUTED, 2)

    # Nein-Bypass
    draw.line([(cx + 260, y_ocr), (cx + 260, y_ocr_do + 26)], fill=MUTED, width=2)
    draw.line([(cx + 260, y_ocr_do + 26), (cx + 150, y_ocr_do + 26)], fill=MUTED, width=2)

    y_trimmed = y_ocr_do + 92
    diamond(draw, cx, y_trimmed, 280, 70, ".trimmed.txt\nvorhanden?", CARD, LAVENDER, LAVENDER, fn)
    draw_arrow(draw, cx + 140, y_trimmed, cx + 260, y_trimmed, MUTED, 2, "Ja", fns)
    draw_arrow(draw, cx, y_trimmed + 35, cx, y_trimmed + 85, MUTED, 2, "Nein", fns)

    y_trimbox = y_trimmed + 110
    box(draw, cx - 0, y_trimbox, 340, 52,
        "Keyword-Filter\nAnleger, MiFID, UCITS, Mindestanlage …",
        CARD, BORDER, TEXT, fns)

    box(draw, cx + 310, y_trimmed, 200, 52,
        "Trimmed-Text\nverwenden (bevorzugt)", CARD, LAVENDER, LAVENDER, fns)
    draw_arrow(draw, cx + 260, y_trimmed + 10, cx + 260, y_trimbox + 10, MUTED, 2)
    draw.line([(cx + 260, y_trimbox + 10), (cx + 170, y_trimbox + 10)], fill=MUTED, width=2)

    draw_arrow(draw, cx, y_trimbox + 26, cx, y_trimbox + 70, MUTED, 2)

    y_tables = y_trimbox + 95
    box(draw, cx, y_tables, 380, 60,
        "Tabellen-Extraktion\nAnteilsklassen-Tabellen (ISIN, TER, Währung …)\n→ .tables.json", CARD, BORDER, TEXT, fns)

    draw_arrow(draw, cx, y_tables + 30, cx, y_tables + 75, MUTED, 2)

    # ═══════════════════════════════════════════════════════════
    # LLM-ANALYSE
    # ═══════════════════════════════════════════════════════════
    y = y_tables + 100
    section_label(y, "LLM-ANALYSE — Anthropic Claude oder Google Gemini", PURPLE)
    y += 28

    box(draw, cx, y, 420, 52,
        "Text + Tabellen an LLM senden (max. 80.000 Zeichen)",
        CARD, PURPLE, TEXT, fn, fnb)

    draw_arrow(draw, cx, y + 26, cx, y + 70, MUTED, 2)

    y += 90
    diamond(draw, cx, y, 220, 70, "Provider?", CARD, MUTED, MUTED, fn)

    # Anthropic Links
    y_llm = y + 30
    cx_ant = cx - 220
    cx_gem = cx + 220
    draw_arrow(draw, cx - 110, y, cx_ant, y_llm, MUTED, 2, "Anthropic", fns)
    draw_arrow(draw, cx + 110, y, cx_gem, y_llm, MUTED, 2, "Gemini", fns)

    box(draw, cx_ant, y_llm + 20, 260, 80,
        "Claude API\nHaiku (Batch, günstig)\nSonnet (Einzel, besser)\nOpus (schwierige Fälle)",
        CARD, PURPLE, PURPLE, fns)

    box(draw, cx_gem, y_llm + 20, 240, 80,
        "Gemini API\nFlash (Batch, schnell)\nPro (Einzel, präziser)",
        CARD, CYAN, CYAN, fns)

    # Rate-Limit-Retry
    y_rate = y_llm + 130
    diamond(draw, cx, y_rate, 220, 70, "Rate Limit?", CARD, RED, RED, fn)

    draw.line([(cx_ant, y_llm + 60), (cx_ant, y_rate)], fill=MUTED, width=2)
    draw.line([(cx_ant, y_rate), (cx - 110, y_rate)], fill=MUTED, width=2)
    draw.line([(cx_gem, y_llm + 60), (cx_gem, y_rate)], fill=MUTED, width=2)
    draw.line([(cx_gem, y_rate), (cx + 110, y_rate)], fill=MUTED, width=2)

    draw_arrow(draw, cx + 110, y_rate, cx + 280, y_rate, MUTED, 2, "Nein", fns)
    draw_arrow(draw, cx, y_rate + 35, cx, y_rate + 90, MUTED, 2, "Ja", fns)

    y_retry = y_rate + 115
    box(draw, cx, y_retry, 360, 70,
        "Warten & Retry\n2h Pause, max. 6 Versuche\nCountdown im Log sichtbar",
        CARD, RED, YELLOW, fns, fnb)

    # Retry-Pfeil zurück nach oben
    draw.line([(cx - 180, y_retry), (cx - 340, y_retry)], fill=MUTED, width=2)
    draw.line([(cx - 340, y_retry), (cx - 340, y_llm + 20)], fill=MUTED, width=2)
    draw.line([(cx - 340, y_llm + 20), (cx_ant - 130, y_llm + 20)], fill=MUTED, width=2)
    draw_arrow(draw, cx_ant - 130, y_llm + 20, cx_ant - 130, y_llm + 10, MUTED, 2)

    # Nein-Zweig
    draw.line([(cx + 280, y_rate), (cx + 280, y_rate + 115)], fill=MUTED, width=2)

    y_parse = y_retry + 95
    draw_arrow(draw, cx, y_retry + 35, cx, y_parse, MUTED, 2)
    draw.line([(cx + 280, y_rate + 115), (cx + 280, y_parse + 26)], fill=MUTED, width=2)
    draw.line([(cx + 280, y_parse + 26), (cx + 160, y_parse + 26)], fill=MUTED, width=2)

    # ═══════════════════════════════════════════════════════════
    # ERGEBNIS
    # ═══════════════════════════════════════════════════════════
    y = y_parse
    section_label(y - 30, "KLASSIFIZIERUNGSERGEBNIS — Normalisierung & Matching", YELLOW)

    box(draw, cx, y, 380, 60,
        "JSON-Antwort parsen\nsegmentierung, fondstyp, anlegertyp, kundentyp,\nbegruendung, konfidenz, anteilsklassen[]",
        CARD, YELLOW, TEXT, fns, fnb)

    draw_arrow(draw, cx, y + 30, cx, y + 80, MUTED, 2)

    y += 100
    box(draw, cx, y, 400, 80,
        "Segmentierungs-Normalisierung\n"
        '"institutional" / "institutionell" → institutional\n'
        '"retail" / "privat" → retail\n'
        '"qualified" / "qualifiziert" → qualified',
        CARD, BLUE, TEXT, fns)

    draw_arrow(draw, cx, y + 40, cx, y + 90, MUTED, 2)

    y += 110
    box(draw, cx, y, 380, 70,
        "ISIN-Matching\nISIN direkt → Anteilsklassen-Eintrag\nKlassenname → Eintrag\nEinzel-Klasse → direkt",
        CARD, BORDER, TEXT, fns)

    draw_arrow(draw, cx, y + 35, cx, y + 85, MUTED, 2)

    y += 105
    box(draw, cx, y, 340, 52,
        "DB speichern\nupdate_llm_analysis (alle ISINs der Gruppe)",
        CARD, LAVENDER, TEXT, fns, fnb)

    draw_arrow(draw, cx, y + 26, cx, y + 70, MUTED, 2)

    y += 90
    # Ergebnis-Ausgabe — 3 Farben
    cx_inst = cx - 220
    cx_ret  = cx
    cx_unk  = cx + 220

    box(draw, cx_inst, y, 180, 52, "institutional\n→ Blau", CARD, BLUE, BLUE, fns)
    box(draw, cx_ret, y, 180, 52, "retail\n→ Grün", CARD, GREEN, GREEN, fns)
    box(draw, cx_unk, y, 180, 52, "qualified / mixed /\nunklar → Gelb", CARD, YELLOW, YELLOW, fns)

    draw.line([(cx - 6, y - 18), (cx_inst, y - 18)], fill=MUTED, width=2)
    draw.line([(cx_inst, y - 18), (cx_inst, y - 26)], fill=MUTED, width=2)
    draw_arrow(draw, cx_inst, y - 26, cx_inst, y - 26, MUTED, 2)

    draw.line([(cx - 6, y - 18), (cx_unk, y - 18)], fill=MUTED, width=2)
    draw.line([(cx_unk, y - 18), (cx_unk, y - 26)], fill=MUTED, width=2)
    draw_arrow(draw, cx - 6, y - 18, cx, y - 26, MUTED, 2)

    # Konfidenz-Hinweis
    y += 80
    box(draw, cx, y, 360, 52,
        "Konfidenz-Anzeige:\n'niedrig' → gelb hervorheben in UI",
        CARD, YELLOW, YELLOW, fns)

    draw_arrow(draw, cx, y + 26, cx, y + 70, MUTED, 2)

    y += 90
    terminal(draw, cx, y, 220, 44, "Ergebnis-Fenster anzeigen", PANEL, GREEN, GREEN, fnb)

    # ── Legende ──────────────────────────────────────────────────────────────
    y += 70
    draw.line([(40, y), (W - 40, y)], fill=BORDER, width=1)
    y += 16
    draw.text((40, y), "Legende:", fill=MUTED, font=fnb)

    items = [
        (BLUE,    "Download / API"),
        (GREEN,   "Erfolg / Retail"),
        (RED,     "Fehler"),
        (YELLOW,  "Warnung / Niedrige Konfidenz"),
        (PURPLE,  "LLM / Claude"),
        (CYAN,    "Gemini / OCR"),
        (LAVENDER,"Datenbank"),
        (MUTED,   "Überspringen"),
    ]
    xleg = 40
    for color, label in items:
        y += 28
        draw.rounded_rectangle([xleg, y, xleg + 18, y + 18], radius=4, fill=color)
        draw.text((xleg + 26, y + 1), label, fill=TEXT, font=fns)
        xleg += 200
        if xleg > W - 200:
            xleg = 40

    # ── Crop zum tatsächlichen Inhalt ─────────────────────────────────────────
    actual_h = y + 60
    img = img.crop((0, 0, W, actual_h))
    img.save(out_path, "PNG", optimize=True)
    print(f"Gespeichert: {out_path}  ({W} x {actual_h} px)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "docs/flowchart.png"
    generate(out)
