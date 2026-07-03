"""
Performance-Vergleich — vergleicht zwei Anteilsklassen anhand von
Factsheets und Jahresberichten per LLM-Analyse.

Ausgabe: strukturierter PDF-Onepager auf Deutsch.
"""

import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
from dotenv import load_dotenv

import pdf_analyzer
import results_store
from utils import logger

load_dotenv()

# ─── Konstanten ──────────────────────────────────────────────────────────────
BG_MAIN         = "#1e1e2e"
BG_PANEL        = "#2a2a3e"
BG_INPUT        = "#313145"
FG_TEXT         = "#cdd6f4"
FG_MUTED        = "#7f849c"
ACCENT_BLUE     = "#89b4fa"
ACCENT_GREEN    = "#a6e3a1"
ACCENT_RED      = "#f38ba8"
ACCENT_YELLOW   = "#f9e2af"
ACCENT_LAVENDER = "#b4befe"
BTN_BG          = "#45475a"
BTN_ACTIVE      = "#585b70"

_DOCS_DIR = Path(__file__).parent.parent / "data" / "comparisons" / "docs"
_OUT_DIR  = Path(__file__).parent.parent / "data" / "comparisons"

_MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-8",
]

_DOC_TYPES = [
    ("factsheet", "Factsheet"),
    ("annual",    "Jahresbericht"),
    ("halfyear",  "Halbjahresbericht"),
]

_SYSTEM_PROMPT = """\
Du bist ein erfahrener Wertpapierspezialist und unabhängiger Portfolio-Analyst \
mit tiefgreifenden Kenntnissen in der quantitativen und qualitativen \
Fondsanalyse.

AUFGABE: Vergleiche die beiden vorliegenden Anteilsklassen anhand der \
bereitgestellten Fondsdokumente (Factsheets, Jahres- und Halbjahresberichte) \
und erkläre präzise und professionell, welche Anteilsklasse die bessere \
Performance aufweist und WARUM.

ANALYSE-SCHWERPUNKTE (nutze ausschliesslich konkrete Zahlen aus den Dokumenten):
• Performance-Zahlen: Rendite 1J/3J/5J, Benchmark-Vergleich, Alpha, Tracking Error
• Kostenstruktur: TER, Ausgabeaufschlag, Performancegebühren, Transaktionskosten
• Portfolio-Unterschiede: Top-Positionen, Sektorgewichtung, geografische Allokation
• Risikokennzahlen: Volatilität, Sharpe Ratio, Max. Drawdown, Beta
• Qualitative Faktoren: Managementstil, Investmentstrategie, Portfoliogrösse

Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt ohne weiteren Text:
{
  "klasse_a_name": "Kurzbezeichnung Anteilsklasse A (max 55 Zeichen)",
  "klasse_b_name": "Kurzbezeichnung Anteilsklasse B (max 55 Zeichen)",
  "besser_performend": "A" oder "B" oder "vergleichbar",
  "performance_attribution": "Kernanalyse: Warum performt eine Klasse besser? 4-5 präzise Sätze mit konkreten Zahlen aus den Dokumenten. Professionell, faktenbasiert, analytisch. (max 600 Zeichen)",
  "hauptgruende": [
    "Hauptgrund 1 mit konkreter Zahl (max 90 Zeichen)",
    "Hauptgrund 2 mit konkreter Zahl (max 90 Zeichen)",
    "Hauptgrund 3 (max 90 Zeichen)",
    "Optionaler Grund 4 (max 90 Zeichen)"
  ],
  "kostenvergleich": "TER-Vergleich und weitere Kostenunterschiede. 1-2 präzise Sätze. (max 200 Zeichen)",
  "risikoprofil": "Risikounterschiede mit konkreten Kennzahlen. 1-2 Sätze. (max 200 Zeichen)",
  "fazit": "Prägnanter Schlusssatz mit klarer analytischer Empfehlung. (max 140 Zeichen)"
}
"""


# ─── Worker ──────────────────────────────────────────────────────────────────

class _ComparisonWorker(threading.Thread):
    """Hintergrund-Worker: lädt Dokumente, extrahiert Text, ruft LLM auf."""

    def __init__(
        self,
        state_a: dict,
        state_b: dict,
        model: str,
        api_key: str,
        result_queue: queue.Queue,
    ):
        super().__init__(daemon=True)
        self._state_a = state_a
        self._state_b = state_b
        self._model = model
        self._api_key = api_key
        self._queue = result_queue
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _emit(self, evt_type: str, payload=None):
        self._queue.put((evt_type, payload))

    def _collect_docs(self, state: dict, side: str) -> tuple[dict, list[str]]:
        """
        Holt Dokumente für eine Seite: fundinfo-Download + Fallback auf
        bestehenden Prospekt + manuell hinzugefügte Dateien.
        Returns (updated_docs_dict, [local_paths]).
        """
        isin = state["isin"].strip()
        docs = dict(state.get("docs", {}))
        manual = list(state.get("manual", []))

        # 1. Fundinfo-Download (nur wenn ISIN vorhanden und Dokumente fehlen)
        if isin and not any(docs.values()):
            self._emit("log", f"[{side}] Suche Dokumente für {isin} auf fundinfo.com …")
            try:
                from fundinfo_client import fetch_comparison_docs
                isin_dir = _DOCS_DIR / isin
                result = fetch_comparison_docs(isin, str(isin_dir), delay=1.5)

                docs["factsheet"] = result.get("factsheet")
                docs["annual"]    = result.get("annual")
                docs["halfyear"]  = result.get("halfyear")

                found = [k for k, v in docs.items() if v]
                missing = [k for k, v in docs.items() if not v]
                if found:
                    self._emit("log",
                        f"[{side}] Gefunden: {', '.join(found)}" +
                        (f"  |  Nicht gefunden: {', '.join(missing)}" if missing else "")
                    )
                else:
                    self._emit("log",
                        f"[{side}] Keine Dokumente auf fundinfo (Keys: "
                        f"{result.get('available_keys', [])})"
                    )
                self._emit("docs_updated", (side, docs))

            except Exception as exc:
                self._emit("log", f"[{side}] fundinfo-Fehler: {exc}")

        # 2. Prospekt-Fallback aus DB (falls noch kein Dokument)
        if isin and not any(docs.values()) and not manual:
            try:
                rows = results_store.get_all_results()
                row = next((r for r in rows if r.get("isin") == isin), None)
                if row and row.get("prospekt_pfad") and Path(row["prospekt_pfad"]).exists():
                    manual = [row["prospekt_pfad"]]
                    self._emit("log",
                        f"[{side}] Verwende Prospekt aus DB: "
                        f"{Path(row['prospekt_pfad']).name}"
                    )
            except Exception:
                pass

        all_paths = [p for p in docs.values() if p] + [p for p in manual if p]
        return docs, all_paths

    def _extract_text(self, paths: list[str], side: str) -> str:
        """Extrahiert und kombiniert Text aus allen Dokumenten einer Seite."""
        parts = []
        for path in paths:
            try:
                self._emit("log", f"[{side}] Extrahiere: {Path(path).name}")
                text, _ = pdf_analyzer.extract_relevant_text(path)
                text = text or ""
                if text:
                    parts.append(f"[Dokument: {Path(path).name}]\n{text[:40_000]}")
                else:
                    self._emit("log", f"[{side}] ⚠ Kein Text: {Path(path).name}")
            except Exception as exc:
                self._emit("log", f"[{side}] Fehler bei {Path(path).name}: {exc}")
        return "\n\n---\n\n".join(parts)

    def _build_prompt(
        self,
        state_a: dict, text_a: str,
        state_b: dict, text_b: str,
    ) -> str:
        def meta_summary(state: dict, text: str) -> str:
            meta = state.get("meta", {})
            isin = state.get("isin", "")
            name = meta.get("name") or state.get("isin", "Anteilsklasse")
            klasse = meta.get("anteilsklasse", "")
            waehrung = meta.get("waehrung", "")
            ter = meta.get("ter", "")
            infos = " | ".join(x for x in [klasse, f"Währung: {waehrung}", f"TER: {ter}"] if x)
            return (
                f"**{name}** (ISIN: {isin})\n"
                f"{infos}\n\n"
                f"### Dokument-Auszüge:\n{text or '(keine Dokumente verfügbar)'}"
            )

        return (
            "## ANTEILSKLASSE A\n\n"
            + meta_summary(state_a, text_a)
            + "\n\n---\n\n"
            + "## ANTEILSKLASSE B\n\n"
            + meta_summary(state_b, text_b)
        )

    def run(self):
        try:
            if self._stop_flag:
                return

            # 1. Dokumente sammeln
            docs_a, paths_a = self._collect_docs(self._state_a, "A")
            docs_b, paths_b = self._collect_docs(self._state_b, "B")

            if self._stop_flag:
                return

            if not paths_a and not paths_b:
                self._emit("error", "Keine Dokumente für beide Seiten verfügbar.")
                self._emit("done", None)
                return

            if not paths_a:
                self._emit("log", "[A] ⚠ Keine Dokumente — Analyse nur mit Seite B möglich")
            if not paths_b:
                self._emit("log", "[B] ⚠ Keine Dokumente — Analyse nur mit Seite A möglich")

            # 2. Text extrahieren
            self._emit("log", "Extrahiere Texte …")
            text_a = self._extract_text(paths_a, "A") if paths_a else ""
            text_b = self._extract_text(paths_b, "B") if paths_b else ""

            if self._stop_flag:
                return

            # 3. LLM aufrufen
            self._emit("log", f"LLM-Analyse startet ({self._model}) …")
            user_msg = self._build_prompt(self._state_a, text_a, self._state_b, text_b)
            char_count = len(user_msg)
            self._emit("log", f"Prompt: {char_count:,} Zeichen")

            client = anthropic.Anthropic(api_key=self._api_key)
            response = client.messages.create(
                model=self._model,
                max_tokens=1500,
                system=[{"type": "text", "text": _SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": user_msg[:150_000],
                     "cache_control": {"type": "ephemeral"}},
                ]}],
            )

            if response.stop_reason == "max_tokens":
                self._emit("log", "⚠ Ausgabe-Limit (max_tokens) erreicht — Antwort möglicherweise abgeschnitten.")

            raw = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            self._emit("log", f"LLM-Antwort: {len(raw)} Zeichen")

            # 4. JSON parsen
            analysis = self._parse_json(raw)
            if not analysis:
                self._emit("error", f"JSON konnte nicht geparst werden:\n{raw[:300]}")
                self._emit("done", None)
                return

            self._emit("result", analysis)
            self._emit("log", "✓ Analyse abgeschlossen")

        except anthropic.AuthenticationError:
            self._emit("error", "Ungültiger API-Key — bitte im Admin konfigurieren.")
        except anthropic.RateLimitError:
            self._emit("error", "Rate Limit erreicht — bitte kurz warten und erneut starten.")
        except anthropic.BadRequestError as exc:
            self._emit("error", f"Eingabe zu lang für Modell-Kontext — Dokumente reduzieren: {exc}")
        except anthropic.APIStatusError as exc:
            self._emit("error", f"API-Fehler {exc.status_code}: {exc.message}")
        except Exception as exc:
            self._emit("error", str(exc))
        finally:
            self._emit("done", None)

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        start, end = text.find("{"), text.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            return None


# ─── PDF-Onepager ─────────────────────────────────────────────────────────────

def _generate_pdf(path: str, analysis: dict, state_a: dict, state_b: dict):
    """Erzeugt einen professionellen PDF-Onepager mit der Vergleichsanalyse."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm, mm
        from reportlab.platypus import (
            HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ImportError:
        raise ImportError(
            "reportlab nicht installiert. Bitte: pip install reportlab"
        )

    Path(path).parent.mkdir(parents=True, exist_ok=True)

    # ── Farben ────────────────────────────────────────────────────────────────
    C_DARK     = colors.HexColor("#1e1e2e")
    C_BLUE     = colors.HexColor("#1d4ed8")
    C_WHITE    = colors.white
    C_BODY     = colors.HexColor("#1f2937")
    C_MUTED    = colors.HexColor("#6b7280")
    C_GREEN_BG = colors.HexColor("#f0fdf4")
    C_BLUE_BG  = colors.HexColor("#eff6ff")
    C_FAZIT_BG = colors.HexColor("#fefce8")
    C_ACCENT   = colors.HexColor("#2563eb")
    C_SEP      = colors.HexColor("#e5e7eb")

    page_w = A4[0]
    usable = page_w - 3 * cm
    col = (usable - 4 * mm) / 2

    # ── Styles ────────────────────────────────────────────────────────────────
    def _style(name, **kw) -> ParagraphStyle:
        base = dict(fontName="Helvetica", fontSize=9, leading=12,
                    textColor=C_BODY, spaceAfter=0)
        base.update(kw)
        return ParagraphStyle(name, **base)

    s_h1     = _style("h1", fontName="Helvetica-Bold", fontSize=13,
                      textColor=C_WHITE, leading=16)
    s_date   = _style("date", fontSize=8, textColor=C_MUTED, alignment=2)
    s_label  = _style("label", fontSize=7.5, textColor=C_MUTED,
                      fontName="Helvetica-Bold")
    s_value  = _style("value", fontSize=8.5, textColor=C_BODY)
    s_isin   = _style("isin", fontSize=7, textColor=C_MUTED, fontName="Helvetica")
    s_sec    = _style("sec", fontName="Helvetica-Bold", fontSize=9,
                      textColor=C_ACCENT, spaceBefore=3)
    s_body   = _style("body", fontSize=8.5, leading=12, textColor=C_BODY)
    s_bullet = _style("bullet", fontSize=8.5, leading=11, leftIndent=10,
                      textColor=C_BODY)
    s_fazit  = _style("fazit", fontName="Helvetica-Bold", fontSize=9,
                      textColor=colors.HexColor("#92400e"), leading=13)
    s_footer = _style("footer", fontSize=7, textColor=C_MUTED, alignment=1)

    # ── Helfer ────────────────────────────────────────────────────────────────
    def p(text: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(str(text or "—"), style)

    def _trunc(text: str, n: int) -> str:
        return text[:n] + "…" if len(text) > n else text

    # ── Datenvorbereitung ────────────────────────────────────────────────────
    meta_a = state_a.get("meta", {})
    meta_b = state_b.get("meta", {})
    name_a = analysis.get("klasse_a_name") or meta_a.get("name") or state_a.get("isin", "A")
    name_b = analysis.get("klasse_b_name") or meta_b.get("name") or state_b.get("isin", "B")
    besser = analysis.get("besser_performend", "")
    besser_label = {"A": f"▲ {_trunc(name_a, 30)}",
                    "B": f"▲ {_trunc(name_b, 30)}",
                    "vergleichbar": "≈ Vergleichbar"}.get(besser, "—")
    heute = datetime.now().strftime("%d.%m.%Y")

    story = []

    # ── 1. Header-Balken ──────────────────────────────────────────────────────
    header_data = [[
        p("Performance-Analyse", s_h1),
        p(f"FundsPilot  ·  {heute}", s_date),
    ]]
    header_tbl = Table(header_data, colWidths=[usable * 0.65, usable * 0.35])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (0, -1), 10),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 4 * mm))

    # ── 2. Fonds-Info-Karten (A | B) ─────────────────────────────────────────
    def _fund_card(state: dict, meta: dict, name: str, side_label: str, bg) -> list:
        isin = state.get("isin", "")
        rows = [
            [p(f"ANTEILSKLASSE {side_label}", s_label), ""],
            [p(_trunc(name, 50), _style("fn", fontName="Helvetica-Bold",
                                        fontSize=9.5, textColor=C_BODY,
                                        leading=12)), ""],
            [p(isin, s_isin), ""],
            [p("Anteilsklasse", s_label), p(meta.get("anteilsklasse", "—"), s_value)],
            [p("Währung", s_label),       p(meta.get("waehrung", "—"), s_value)],
            [p("TER", s_label),            p(meta.get("ter", "—"), s_value)],
        ]
        tbl = Table(rows, colWidths=[col * 0.42, col * 0.58])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), bg),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("SPAN",          (0, 0), (1, 0)),
            ("SPAN",          (0, 1), (1, 1)),
            ("SPAN",          (0, 2), (1, 2)),
        ]))
        return tbl

    card_a = _fund_card(state_a, meta_a, name_a, "A", C_GREEN_BG)
    card_b = _fund_card(state_b, meta_b, name_b, "B", C_BLUE_BG)

    cards_row = Table([[card_a, card_b]], colWidths=[col, col],
                      hAlign="LEFT",
                      colPadding=4)
    cards_row.setStyle(TableStyle([
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ("ALIGN",        (0, 0), (-1, -1), "LEFT"),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(cards_row)
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="100%", thickness=0.8, color=C_SEP))
    story.append(Spacer(1, 3 * mm))

    # ── 3. Performance-Attribution ────────────────────────────────────────────
    story.append(p("Performance-Analyse", s_sec))
    story.append(Spacer(1, 1 * mm))
    story.append(p(
        _trunc(analysis.get("performance_attribution", ""), 650),
        s_body
    ))
    story.append(Spacer(1, 3 * mm))

    # ── 4. Hauptgründe ────────────────────────────────────────────────────────
    gruende = analysis.get("hauptgruende", [])
    if gruende:
        story.append(p("Hauptgründe", s_sec))
        story.append(Spacer(1, 1 * mm))
        for g in gruende[:5]:
            story.append(p(f"• {_trunc(str(g), 100)}", s_bullet))
    story.append(Spacer(1, 3 * mm))

    # ── 5. Kosten | Risiko ────────────────────────────────────────────────────
    kosten = _trunc(analysis.get("kostenvergleich", ""), 220)
    risiko = _trunc(analysis.get("risikoprofil", ""), 220)
    kr_data = [[
        [p("Kostenvergleich", s_sec), Spacer(1, 1 * mm), p(kosten, s_body)],
        [p("Risikoprofil",    s_sec), Spacer(1, 1 * mm), p(risiko, s_body)],
    ]]
    kr_tbl = Table(kr_data, colWidths=[col, col])
    kr_tbl.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(kr_tbl)
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="100%", thickness=0.8, color=C_SEP))
    story.append(Spacer(1, 3 * mm))

    # ── 6. Fazit ─────────────────────────────────────────────────────────────
    fazit_text = (
        f"<b>Ergebnis: {besser_label}</b>  —  "
        f"{_trunc(analysis.get('fazit', ''), 150)}"
    )
    fazit_data = [[p(fazit_text, s_fazit)]]
    fazit_tbl = Table(fazit_data, colWidths=[usable])
    fazit_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_FAZIT_BG),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("ROUNDEDCORNERS", (0, 0), (-1, -1), [4, 4, 4, 4]),
    ]))
    story.append(fazit_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 7. Footer ─────────────────────────────────────────────────────────────
    isin_a = state_a.get("isin", "")
    isin_b = state_b.get("isin", "")
    footer_text = (
        f"Erstellt mit FundsprospectPilot  ·  Quellen: fundinfo.com  ·  "
        f"ISIN A: {isin_a}  ·  ISIN B: {isin_b}  ·  "
        "Diese Analyse dient ausschliesslich zu Informationszwecken und stellt keine Anlageberatung dar."
    )
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_SEP))
    story.append(Spacer(1, 2 * mm))
    story.append(p(footer_text, s_footer))

    # ── Build ─────────────────────────────────────────────────────────────────
    doc = SimpleDocTemplate(
        path, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    doc.build(story)


# ─── ISIN-Picker aus DB ───────────────────────────────────────────────────────

class _ISINPickerDialog(tk.Toplevel):
    """Einfacher Dialog zum Auswählen einer ISIN aus der DB."""

    def __init__(self, parent, callback):
        super().__init__(parent)
        self.title("ISIN aus Datenbank wählen")
        self.configure(bg=BG_MAIN)
        self.geometry("540x480")
        self.resizable(True, True)
        self.grab_set()
        self._callback = callback
        self._rows: list[dict] = []
        self._build_ui()
        self._load()

    def _build_ui(self):
        top = tk.Frame(self, bg=BG_MAIN)
        top.pack(fill="x", padx=12, pady=(10, 4))

        tk.Label(top, text="🔍", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 10)).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter())
        tk.Entry(top, textvariable=self._search_var, bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat",
                 font=("Segoe UI", 10)).pack(
            side="left", fill="x", expand=True, padx=(6, 0), ipady=4)

        frame = tk.Frame(self, bg=BG_MAIN)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        style = ttk.Style(self)
        style.configure("Picker.Treeview", background=BG_PANEL, foreground=FG_TEXT,
                         fieldbackground=BG_PANEL, rowheight=22, font=("Segoe UI", 9))
        style.configure("Picker.Treeview.Heading", background=BG_INPUT,
                         foreground=ACCENT_BLUE, font=("Segoe UI", 9, "bold"))

        self._tree = ttk.Treeview(frame, style="Picker.Treeview",
                                  columns=("isin", "name", "klasse"),
                                  show="headings", selectmode="browse")
        self._tree.heading("isin",   text="ISIN")
        self._tree.heading("name",   text="Fondsname")
        self._tree.heading("klasse", text="Anteilsklasse")
        self._tree.column("isin",   width=120, anchor="w")
        self._tree.column("name",   width=240, anchor="w")
        self._tree.column("klasse", width=140, anchor="w")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)
        self._tree.bind("<Double-1>", self._select)

        tk.Button(self, text="  Auswählen  ", command=self._select,
                  bg="#1e2a3e", fg=ACCENT_BLUE, relief="flat",
                  font=("Segoe UI", 10), padx=12, pady=5, cursor="hand2"
                  ).pack(pady=(0, 10))

    def _load(self):
        try:
            self._rows = results_store.get_all_results()
        except Exception:
            self._rows = []
        self._filter()

    def _filter(self):
        q = self._search_var.get().lower()
        self._tree.delete(*self._tree.get_children())
        for r in self._rows:
            isin = r.get("isin", "")
            name = r.get("fondsname", "") or r.get("subfonds_name", "")
            klasse = r.get("anteilsklasse", "")
            if q and q not in isin.lower() and q not in name.lower():
                continue
            self._tree.insert("", "end", iid=isin,
                              values=(isin, name or "—", klasse or "—"))

    def _select(self, _event=None):
        sel = self._tree.selection()
        if not sel:
            return
        isin = sel[0]
        row = next((r for r in self._rows if r.get("isin") == isin), None)
        self._callback(isin, row)
        self.destroy()


# ─── Hauptfenster ────────────────────────────────────────────────────────────

class ComparisonWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("Performance-Vergleich")
        self.configure(bg=BG_MAIN)
        self.geometry("1080x780")
        self.minsize(860, 620)

        # State: A und B
        self._state: dict[str, dict] = {
            "A": self._empty_state(),
            "B": self._empty_state(),
        }
        self._worker: _ComparisonWorker | None = None
        self._queue: queue.Queue = queue.Queue()
        self._last_analysis: dict | None = None

        self._build_ui()
        self._poll_queue()

    @staticmethod
    def _empty_state() -> dict:
        return {"isin": "", "meta": {}, "docs": {}, "manual": []}

    # ─── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()

        body = tk.Frame(self, bg=BG_MAIN)
        body.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        # Zwei Seiten-Panels nebeneinander
        panels = tk.Frame(body, bg=BG_MAIN)
        panels.pack(fill="x")
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)

        self._ui: dict[str, dict] = {}
        for col, side in enumerate(("A", "B")):
            self._build_side_panel(panels, side, col)

        # Analyse-Bereich
        self._build_analysis_area(body)
        self._build_log(body)

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        tk.Label(inner, text="⚖  Performance-Vergleich",
                 bg=BG_PANEL, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        self._btn_stop = tk.Button(
            inner, text="⏹  Stopp", command=self._stop,
            bg="#3a2000", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE, state="disabled")
        self._btn_stop.pack(side="right", padx=(4, 0))

        self._btn_pdf = tk.Button(
            inner, text="📄  PDF exportieren", command=self._export_pdf,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE, state="disabled")
        self._btn_pdf.pack(side="right", padx=(4, 0))

        self._btn_start = tk.Button(
            inner, text="▶  Analyse starten", command=self._start,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE)
        self._btn_start.pack(side="right", padx=(4, 0))

        self._model_var = tk.StringVar(value=_MODELS[0])
        ttk.Combobox(inner, textvariable=self._model_var, values=_MODELS,
                     state="readonly", width=26, font=("Segoe UI", 9)
                     ).pack(side="right", padx=(2, 0))
        tk.Label(inner, text="Modell:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(10, 2))

    def _build_side_panel(self, parent, side: str, col: int):
        pad = (0, 5) if col == 0 else (5, 0)
        outer = tk.Frame(parent, bg=BG_PANEL)
        outer.grid(row=0, column=col, sticky="nsew", padx=pad, pady=(0, 6))

        # Header
        header = tk.Frame(outer, bg=BG_PANEL)
        header.pack(fill="x", padx=10, pady=(8, 4))
        color = ACCENT_GREEN if side == "A" else ACCENT_BLUE
        tk.Label(header, text=f"Anteilsklasse {side}",
                 bg=BG_PANEL, fg=color,
                 font=("Segoe UI", 10, "bold")).pack(side="left")

        # ISIN-Zeile
        isin_row = tk.Frame(outer, bg=BG_PANEL)
        isin_row.pack(fill="x", padx=10, pady=(0, 4))

        tk.Label(isin_row, text="ISIN:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")

        isin_var = tk.StringVar()
        isin_entry = tk.Entry(isin_row, textvariable=isin_var,
                              bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                              font=("Segoe UI", 9), relief="flat", bd=3, width=16)
        isin_entry.pack(side="left", padx=(4, 4))
        isin_var.trace_add("write", lambda *_: self._on_isin_changed(side))

        tk.Button(isin_row, text="aus DB",
                  command=lambda s=side: self._pick_from_db(s),
                  bg=BTN_BG, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 8), padx=5, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left", padx=(0, 4))

        tk.Button(isin_row, text="🔍 Suchen",
                  command=lambda s=side: self._lookup_isin(s),
                  bg="#1e2a3e", fg=ACCENT_BLUE, relief="flat",
                  font=("Segoe UI", 8), padx=6, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left")

        ttk.Separator(outer, orient="horizontal").pack(fill="x", padx=10, pady=(2, 4))

        # Metadaten-Anzeige
        meta_frame = tk.Frame(outer, bg=BG_PANEL)
        meta_frame.pack(fill="x", padx=10, pady=(0, 4))
        meta_frame.columnconfigure(1, weight=1)

        meta_labels = {}
        meta_fields = [
            ("name",          "Fondsname",      0),
            ("anteilsklasse", "Anteilsklasse",  1),
            ("waehrung",      "Währung",         2),
            ("ter",           "TER",             3),
        ]
        for key, label, row in meta_fields:
            tk.Label(meta_frame, text=f"{label}:", bg=BG_PANEL, fg=FG_MUTED,
                     font=("Segoe UI", 8), anchor="w",
                     width=12).grid(row=row, column=0, sticky="w", pady=1)
            lbl = tk.Label(meta_frame, text="—", bg=BG_PANEL, fg=FG_TEXT,
                           font=("Segoe UI", 8), anchor="w")
            lbl.grid(row=row, column=1, sticky="w", pady=1)
            meta_labels[key] = lbl

        ttk.Separator(outer, orient="horizontal").pack(fill="x", padx=10, pady=(4, 4))

        # Dokument-Status
        tk.Label(outer, text="Dokumente", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", padx=10)

        doc_labels = {}
        for doc_key, doc_name in _DOC_TYPES:
            row = tk.Frame(outer, bg=BG_PANEL)
            row.pack(fill="x", padx=10, pady=1)
            status_lbl = tk.Label(row, text="—", bg=BG_PANEL, fg=FG_MUTED,
                                  font=("Consolas", 9), width=3, anchor="w")
            status_lbl.pack(side="left")
            tk.Label(row, text=doc_name, bg=BG_PANEL, fg=FG_TEXT,
                     font=("Segoe UI", 8), anchor="w").pack(side="left", padx=(2, 0))
            doc_labels[doc_key] = status_lbl

        # Manuelle Dateien
        manual_frame = tk.Frame(outer, bg=BG_PANEL)
        manual_frame.pack(fill="x", padx=10, pady=(4, 0))

        self._manual_listbox_frame = manual_frame

        manual_list = tk.Listbox(
            manual_frame, bg=BG_INPUT, fg=FG_TEXT, selectbackground=BTN_ACTIVE,
            font=("Segoe UI", 8), relief="flat", height=2, bd=0,
            selectforeground=FG_TEXT,
        )
        manual_list.pack(fill="x")

        btn_row = tk.Frame(outer, bg=BG_PANEL)
        btn_row.pack(fill="x", padx=10, pady=(2, 8))
        tk.Button(btn_row, text="+ Datei hinzufügen",
                  command=lambda s=side: self._add_manual(s),
                  bg=BTN_BG, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left")
        tk.Button(btn_row, text="✖",
                  command=lambda s=side, lb=manual_list: self._remove_manual(s, lb),
                  bg=BTN_BG, fg=ACCENT_RED, relief="flat",
                  font=("Segoe UI", 8), padx=4, pady=2, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left", padx=(4, 0))

        # Referenzen speichern
        self._ui[side] = {
            "isin_var":    isin_var,
            "meta_labels": meta_labels,
            "doc_labels":  doc_labels,
            "manual_list": manual_list,
        }

    def _build_analysis_area(self, parent):
        lbl_row = tk.Frame(parent, bg=BG_MAIN)
        lbl_row.pack(fill="x", pady=(4, 2))
        tk.Label(lbl_row, text="Analyse-Ergebnis", bg=BG_MAIN, fg=ACCENT_BLUE,
                 font=("Segoe UI", 10, "bold")).pack(side="left")

        self._result_text = scrolledtext.ScrolledText(
            parent, height=8, bg=BG_PANEL, fg=FG_TEXT,
            font=("Segoe UI", 9), relief="flat", state="disabled", wrap="word")
        self._result_text.pack(fill="both", expand=True, pady=(0, 6))

        self._result_text.tag_config("header",  foreground=ACCENT_LAVENDER,
                                     font=("Segoe UI", 9, "bold"))
        self._result_text.tag_config("label",   foreground=FG_MUTED,
                                     font=("Segoe UI", 8))
        self._result_text.tag_config("value",   foreground=FG_TEXT)
        self._result_text.tag_config("green",   foreground=ACCENT_GREEN,
                                     font=("Segoe UI", 9, "bold"))
        self._result_text.tag_config("yellow",  foreground=ACCENT_YELLOW)
        self._result_text.tag_config("bullet",  foreground=ACCENT_BLUE)

    def _build_log(self, parent):
        tk.Label(parent, text="Log", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w")
        self._log = scrolledtext.ScrolledText(
            parent, height=4, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), relief="flat", state="disabled")
        self._log.pack(fill="x", pady=(0, 8))

    # ─── State-Helpers ────────────────────────────────────────────────────────

    def _on_isin_changed(self, side: str):
        isin = self._ui[side]["isin_var"].get().strip().upper()
        self._state[side]["isin"] = isin

    def _get_state_snapshot(self, side: str) -> dict:
        s = dict(self._state[side])
        s["isin"] = self._ui[side]["isin_var"].get().strip().upper()
        lb = self._ui[side]["manual_list"]
        s["manual"] = list(lb.get(0, "end"))
        return s

    # ─── Aktionen ────────────────────────────────────────────────────────────

    def _lookup_isin(self, side: str):
        isin = self._ui[side]["isin_var"].get().strip().upper()
        if not isin:
            messagebox.showwarning("Kein ISIN", "Bitte zuerst eine ISIN eingeben.",
                                   parent=self)
            return
        if self._worker and self._worker.is_alive():
            messagebox.showwarning("Läuft", "Bitte warten bis die Analyse abgeschlossen ist.",
                                   parent=self)
            return

        self._log_line(f"[{side}] Metadaten für {isin} abrufen …")
        self._state[side]["isin"] = isin

        def fetch():
            try:
                from fundinfo_client import fetch_comparison_docs
                isin_dir = _DOCS_DIR / isin
                result = fetch_comparison_docs(isin, str(isin_dir), delay=1.0)
                meta = result.get("meta", {})
                docs = {
                    "factsheet": result.get("factsheet"),
                    "annual":    result.get("annual"),
                    "halfyear":  result.get("halfyear"),
                }
                self.after(0, lambda: self._apply_lookup_result(side, meta, docs))
            except Exception as exc:
                self.after(0, lambda: self._log_line(f"[{side}] Fehler: {exc}"))

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_lookup_result(self, side: str, meta: dict, docs: dict):
        self._state[side]["meta"] = meta
        self._state[side]["docs"] = docs
        self._update_meta_ui(side, meta)
        self._update_doc_ui(side, docs)
        found = [k for k, v in docs.items() if v]
        self._log_line(
            f"[{side}] {meta.get('name', '—')} | "
            f"Dokumente: {', '.join(found) if found else 'keine gefunden'}"
        )

    def _pick_from_db(self, side: str):
        def on_select(isin: str, row: dict | None):
            self._ui[side]["isin_var"].set(isin)
            self._state[side]["isin"] = isin
            if row:
                meta = {
                    "name":          row.get("fondsname") or row.get("subfonds_name", ""),
                    "anteilsklasse": row.get("anteilsklasse", ""),
                    "waehrung":      row.get("fondswaehrung", ""),
                    "ter":           row.get("fundinfo_ter", ""),
                }
                self._state[side]["meta"] = meta
                self._update_meta_ui(side, meta)
                # Bestehenden Prospekt als Fallback-Dokument setzen
                pfad = row.get("prospekt_pfad", "")
                if pfad and Path(pfad).exists():
                    lb = self._ui[side]["manual_list"]
                    if pfad not in lb.get(0, "end"):
                        lb.insert("end", pfad)
                    self._log_line(f"[{side}] {isin} aus DB — Prospekt als Dokument gesetzt")
        _ISINPickerDialog(self, on_select)

    def _add_manual(self, side: str):
        paths = filedialog.askopenfilenames(
            parent=self,
            title="PDF-Dokument(e) hinzufügen",
            filetypes=[("PDF", "*.pdf"), ("Alle Dateien", "*.*")],
        )
        lb = self._ui[side]["manual_list"]
        existing = set(lb.get(0, "end"))
        for p in paths:
            if p not in existing:
                lb.insert("end", p)
                self._log_line(f"[{side}] Dokument hinzugefügt: {Path(p).name}")

    def _remove_manual(self, side: str, lb: tk.Listbox):
        sel = lb.curselection()
        if sel:
            lb.delete(sel[0])

    def _update_meta_ui(self, side: str, meta: dict):
        labels = self._ui[side]["meta_labels"]
        for key in ("name", "anteilsklasse", "waehrung", "ter"):
            val = meta.get(key, "") or "—"
            if key == "name" and len(val) > 40:
                val = val[:38] + "…"
            labels[key].config(text=val)

    def _update_doc_ui(self, side: str, docs: dict):
        labels = self._ui[side]["doc_labels"]
        for doc_key, _ in _DOC_TYPES:
            path = docs.get(doc_key)
            if path:
                labels[doc_key].config(text="✓", fg=ACCENT_GREEN)
            else:
                labels[doc_key].config(text="—", fg=FG_MUTED)

    # ─── Analyse ─────────────────────────────────────────────────────────────

    def _start(self):
        if self._worker and self._worker.is_alive():
            return

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            messagebox.showerror("API-Key fehlt",
                                 "Kein ANTHROPIC_API_KEY konfiguriert.",
                                 parent=self)
            return

        isin_a = self._ui["A"]["isin_var"].get().strip().upper()
        isin_b = self._ui["B"]["isin_var"].get().strip().upper()
        if not isin_a and not isin_b:
            messagebox.showwarning("Keine ISIN",
                                   "Bitte mindestens eine ISIN eingeben.",
                                   parent=self)
            return

        state_a = self._get_state_snapshot("A")
        state_b = self._get_state_snapshot("B")

        self._last_analysis = None
        self._btn_pdf.config(state="disabled")
        self._clear_result()
        self._log_line("▶ Analyse gestartet …")

        self._queue = queue.Queue()
        self._worker = _ComparisonWorker(
            state_a=state_a,
            state_b=state_b,
            model=self._model_var.get(),
            api_key=api_key,
            result_queue=self._queue,
        )
        self._worker.start()
        self._set_running(True)

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self._log_line("⏹ Wird gestoppt …")

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self._btn_start.config(state=state)
        self._btn_stop.config(state="normal" if running else "disabled")
        if hasattr(self.master, "notify_process"):
            self.master.notify_process("Vergleich", running)

    # ─── Queue-Polling ────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                evt_type, payload = self._queue.get_nowait()
                if evt_type == "log":
                    self._log_line(payload)
                elif evt_type == "error":
                    self._log_line(f"❌ {payload}")
                elif evt_type == "docs_updated":
                    side, docs = payload
                    self._state[side]["docs"] = docs
                    self._update_doc_ui(side, docs)
                elif evt_type == "result":
                    self._last_analysis = payload
                    self._show_result(payload)
                    self._btn_pdf.config(state="normal")
                elif evt_type == "done":
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    # ─── Ergebnis anzeigen ────────────────────────────────────────────────────

    def _clear_result(self):
        self._result_text.config(state="normal")
        self._result_text.delete("1.0", "end")
        self._result_text.config(state="disabled")

    def _show_result(self, a: dict):
        self._result_text.config(state="normal")
        self._result_text.delete("1.0", "end")

        besser = a.get("besser_performend", "")
        besser_name = {
            "A": a.get("klasse_a_name", "A"),
            "B": a.get("klasse_b_name", "B"),
        }.get(besser, "")

        if besser_name:
            self._result_text.insert("end",
                f"▲ Besser performend: {besser_name}\n\n", "green")
        else:
            self._result_text.insert("end", "≈ Vergleichbar\n\n", "yellow")

        self._result_text.insert("end", "Performance-Attribution\n", "header")
        self._result_text.insert("end",
            a.get("performance_attribution", "") + "\n\n", "value")

        gruende = a.get("hauptgruende", [])
        if gruende:
            self._result_text.insert("end", "Hauptgründe\n", "header")
            for g in gruende:
                self._result_text.insert("end", f"  • {g}\n", "bullet")
            self._result_text.insert("end", "\n")

        self._result_text.insert("end", "Kostenvergleich  ", "label")
        self._result_text.insert("end",
            a.get("kostenvergleich", "") + "\n\n", "value")

        self._result_text.insert("end", "Risikoprofil  ", "label")
        self._result_text.insert("end",
            a.get("risikoprofil", "") + "\n\n", "value")

        self._result_text.insert("end", "Fazit\n", "header")
        self._result_text.insert("end",
            a.get("fazit", ""), "yellow")

        self._result_text.config(state="disabled")

    # ─── PDF-Export ───────────────────────────────────────────────────────────

    def _export_pdf(self):
        if not self._last_analysis:
            messagebox.showinfo("Kein Ergebnis",
                                "Bitte zuerst eine Analyse durchführen.",
                                parent=self)
            return

        isin_a = self._state["A"].get("isin", "A")
        isin_b = self._state["B"].get("isin", "B")
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        default_name = f"Vergleich_{isin_a}_vs_{isin_b}_{ts}.pdf"

        path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialdir=str(_OUT_DIR),
            initialfile=default_name,
            title="Onepager speichern",
        )
        if not path:
            return

        try:
            _OUT_DIR.mkdir(parents=True, exist_ok=True)
            _generate_pdf(
                path=path,
                analysis=self._last_analysis,
                state_a=self._get_state_snapshot("A"),
                state_b=self._get_state_snapshot("B"),
            )
            messagebox.showinfo("PDF gespeichert",
                                f"Onepager gespeichert:\n{path}",
                                parent=self)
            os.startfile(path)
        except ImportError as e:
            messagebox.showerror("Abhängigkeit fehlt",
                                 f"{e}\n\nBitte: pip install reportlab",
                                 parent=self)
        except Exception as e:
            messagebox.showerror("PDF-Fehler", str(e), parent=self)

    # ─── Log ─────────────────────────────────────────────────────────────────

    def _log_line(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")
