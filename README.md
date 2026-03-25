# FundsprospectPilot

Batch-Tool zur automatischen Klassifizierung von Fondsprospekten (institutional / retail) via Claude AI.

## Was es macht

1. Liest ISINs und Fondsnamen aus einer Excel-Datei
2. Sucht den Verkaufsprospekt auf fundinfo.com (JSON-API, automatischer Sprachfallback DE → EN → FR)
3. Extrahiert den relevanten Text aus der PDF
4. Klassifiziert per Claude AI: Segmentierung, Fondstyp, Anlegertyp, Kundentyp
5. Schreibt Ergebnis crashsicher zurück in die Excel-Datei (nach jeder ISIN)
6. Bei niedriger Konfidenz oder fehlendem PDF: automatische Web-Suche als Fallback

## Voraussetzungen

- Python 3.11 oder neuer
- Anthropic API-Key (console.anthropic.com)

## Einrichtung

```bash
# 1. Abhängigkeiten installieren
pip install -r requirements.txt

# 2. Konfiguration anlegen
cp .env.example .env
# .env öffnen und ANTHROPIC_API_KEY eintragen

# 3. Excel-Datei in data/input/ ablegen
#    Spalten: A=ISIN, B=GroupInvestment (Fondsname), C=Morningstar_Segmentierung
```

Unter Windows alternativ `setup.bat` doppelklicken.

## Verwendung

### GUI (empfohlen)

```bash
python src/app.py
```

Oder `start.bat` doppelklicken.

### Batch-Modus (Kommandozeile)

```bash
python src/main.py
```

### Nur eine PDF analysieren

Im GUI: "Einzelne PDF analysieren" Panel verwenden.

## Excel-Struktur

| Spalte | Inhalt | Beschreibung |
|--------|--------|--------------|
| A | ISIN | Pflicht |
| B | GroupInvestment | Fondsname |
| C | Morningstar_Segmentierung | Bestehende Klassifizierung (wird nicht überschrieben) |
| G | Claude_Segmentierung | **Ausgabe:** institutional / retail / unklar |
| H | Fondstyp | **Ausgabe:** UCITS / AIF / ETF / ... |
| I | Anlegertyp | **Ausgabe:** Professionelle Anleger / Privatanleger / ... |
| J | Kundentyp | **Ausgabe:** MiFID Professional / MiFID Retail / ... |
| K | Konfidenz | **Ausgabe:** hoch / mittel / niedrig |
| L | PDF_Dateiname | **Ausgabe:** lokaler Dateiname |
| M | Verarbeitungsstatus | **Ausgabe:** ok / fehler |

Bereits verarbeitete Zeilen (Status = "ok") werden beim nächsten Lauf übersprungen.

## Kosten-Übersicht

| Modell | Kosten/MTok Input | Empfehlung |
|--------|-------------------|------------|
| claude-haiku-4-5 | $1 | Batch (Standard) |
| claude-sonnet-4-6 | $3 | Einzel-PDF-Analyse |
| claude-opus-4-6 | $15 | Schwierige Fälle |

Für 77.000 ISINs mit Haiku (Prospekt ~80 Seiten, ~40k Zeichen Textauszug):
ca. **$300–600** je nach Dokumentdichte.

## fundinfo.com API

Das Tool nutzt den öffentlichen JSON-Endpoint:

```
GET https://www.fundinfo.com/en/{profile}/LandingPage/Data
    ?skip=0&query={ISIN}&orderdirection=desc
```

Profil-Reihenfolge bei Fallback: `CH-prof` → `CH-pub` → `DE-prof` → `LU-prof` → `AT-prof`

## Konfiguration (Env-Variablen)

Alle Optionen in `.env.example` dokumentiert. Wichtigste:

```
ANTHROPIC_API_KEY      = sk-ant-...
EXCEL_PATH             = Pfad zur Excel-Datei
PDF_FOLDER             = Zielordner für PDFs
BATCH_SIZE             = 200  (ISINs pro Lauf)
CLAUDE_BATCH_MODEL     = claude-haiku-4-5-20251001
CLAUDE_SINGLE_MODEL    = claude-sonnet-4-6
```

## Projektstruktur

```
FundsprospectPilot/
├── src/
│   ├── app.py              ← Tkinter GUI (Haupteinstieg)
│   ├── main.py             ← Batch-Engine
│   ├── fundinfo_client.py  ← fundinfo.com JSON-API
│   ├── claude_classifier.py← Claude AI Klassifizierung
│   ├── pdf_analyzer.py     ← PDF-Textextraktion (pdfplumber)
│   ├── excel_handler.py    ← Excel lesen/schreiben (openpyxl)
│   ├── web_search.py       ← DuckDuckGo Fallback-Suche
│   └── utils.py            ← Logging, Hilfsfunktionen
├── data/
│   ├── input/              ← Excel-Datei hier ablegen
│   ├── output/             ← Logs
│   └── prospectus/         ← Heruntergeladene PDFs
├── .env.example            ← Konfigurationsvorlage
├── requirements.txt
├── setup.bat               ← Windows: einmalige Installation
└── start.bat               ← Windows: GUI starten
```
