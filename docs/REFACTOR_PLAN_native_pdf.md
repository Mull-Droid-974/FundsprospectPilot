# Umbau-Plan: Native-PDF-Analysepipeline

**Erstellt:** 2026-07-02 · Status: **Planung (noch nicht umgesetzt)**

## 1. Zielbild & festgelegte Entscheidungen

| Thema | Entscheidung |
|---|---|
| Modelle | **Nur Sonnet 4.6 / Opus 4.8** — Haiku überall entfernen |
| Provider | **Gemini/OpenRouter bleiben** als Option (nur Haiku raus) |
| Analyse-Input | **Immer natives PDF** (Vision — Bilder/Tabellen/Grafiken bleiben erhalten) |
| Große PDFs (>600 S. / >32 MB) | **Reduziertes natives PDF** (relevante Seiten), erzeugt aus dem Original-PDF |
| Ausführung | **Batch (−50%, async) ODER synchron** — GUI-Umschalter pro Lauf, Default Batch |
| PDF-Zustellung | **Base64 inline** im Request |

**Motivation:** Die bisherige Text-Extraktion (pdfplumber) verliert Bilder/Grafiken/komplexe Tabellen und der zeilenbasierte Keyword-Filter verliert Kontext. Natives PDF gibt Claude die Seite visuell → kein Extraktionsverlust.

---

## 2. Neue Architektur

```
PDF ──> Routing (Seitenzahl + MB aus Metadaten, ohne Volltext)
        │
        ├─ ≤600 Seiten UND ≤32 MB ──> natives PDF direkt (base64)
        │
        └─ >600 Seiten ODER >32 MB ──> Seiten-Reduktion (Trim)
                                        │  relevante Seiten wählen (Seiten-Level,
                                        │  ausgebauter Keyword-/Tabellen-Detektor)
                                        │  → reduziertes PDF (pypdf), ≤600 S/≤32 MB
                                        └─> reduziertes natives PDF (base64), gecacht als .reduced.pdf
        │
        ▼
   Analyse-Request: document-Block (base64 PDF) + ISIN-Liste (Text)
        │
        ├─ Batch-Pfad (messages.batches.create, −50%, async)   ← Default
        └─ Sync-Pfad (messages.create im Hintergrund-Worker)
        │
        ▼
   Parsing + Taxonomie-Validierung + Speichern (unverändert)
```

**Kernprinzip Trim:** Es wird **wörtlich auf Seitenebene ausgewählt** (welche Seiten relevant sind) und diese Seiten **nativ** weitergegeben — nie Text umgeschrieben. Verlustfrei auf den gewählten Seiten; einziges Restrisiko ist Seitenauswahl (durch großzügige Auswahl + Nachbarseiten minimiert).

---

## 3. Komponenten: neu / geändert / entfernt

### Neu
- **`pdf_native.py`** (neu): `pdf_to_document_block(pdf_path)` → base64-`document`-Block; `pdf_metadata_light(pdf_path)` (Seiten+MB ohne Volltext); `reduce_pdf_to_relevant_pages(pdf_path)` → `.reduced.pdf` (pypdf), inkl. Seiten-Relevanz-Auswahl.
- **Seiten-Relevanz-Auswahl** (Ausbau des Keyword-Filters): pro Seite prüfen auf relevante Keywords **und** relevante Tabellen (bestehende `_RELEVANT_TABLE_KEYWORDS`), ganze Seiten + Nachbarn behalten. Ersetzt den zeilenbasierten `extract_relevant_sections`.

### Geändert
- **`batch_analysis.py`**: `submit_batch` baut `document`-Blöcke (base64 PDF) statt Text; Blockgröße nach **kumulierten MB** (256-MB-Batch-Limit) statt nur Anzahl.
- **`llm_analysis_worker.py`**: `prepare_pdf_text` → `prepare_pdf_document` (liefert document-Block statt Text); `build_analysis_messages` nimmt document-Block; Sync-Worker sendet natives PDF.
- **`llm_provider.py`**: Haiku aus `MODELS`, `DEFAULT_BATCH_MODELS`, `DEFAULT_SINGLE_MODELS` entfernen; Batch- und Single-Modell auf Sonnet. Provider-spezifische PDF-Übergabe (s. Risiken).
- **`claude_classifier.py`**: auf `document`-Block umstellen; Haiku-Referenzen raus.
- **`.env`**: `CLAUDE_BATCH_MODEL`/`CLAUDE_SINGLE_MODEL` → Sonnet.

### Entfernen / obsolet
- Text-Analysepfad: `prepare_pdf_text` (Text), `extract_relevant_text`, zeilenbasierter `extract_relevant_sections`, `.trimmed.txt` / `.extracted.json` / `.tables.json`.
- LLM-Text-Trim (`pdf_trim_window._run_trim_headless` / `_run_extraction_headless`) → ersetzt durch `reduce_pdf_to_relevant_pages` (keine LLM-Kosten beim Reduzieren, nur Seitenauswahl).
- OCR (`_try_ocr`) für den Analysepfad — natives PDF liest Scans visuell. (Nur behalten, falls anderweitig genutzt.)
- Haiku-Einträge in allen Dropdowns/Configs.

---

## 4. GUI-Aufräumen

| Fenster | Änderung |
|---|---|
| `prospekt_analysis_window.py` | Modell-Dropdown: **Haiku raus** (nur Sonnet/Opus). **Umschalter Batch ↔ synchron** (Default Batch). Native-PDF-Flow verdrahten; alte Text-/Trim-Fenster-Abhängigkeiten entfernen. Kosten-Schätzung pro Lauf **vor** dem Einreichen anzeigen (native PDF ist teuer). |
| `pdf_trim_window.py` | Vom LLM-Text-Trim auf **„PDF auf relevante Seiten reduzieren"** umbauen (oder als Standalone entfernen und in die Pipeline falten). Die vielen synchronen `messages.create`-Stellen bereinigen. |
| `admin_panel.py` | Modell-Konfiguration: Haiku-Defaults entfernen; Batch-/Single-Modell = Sonnet. |
| `comparison_window.py` | Auf natives PDF umstellen; Haiku raus. |

---

## 5. Kosten & Limits ⚠️

- **Natives PDF ist deutlich teurer:** ~1.500–3.000 **Text**-Token **plus Bild-Token pro Seite**. Ein Prospekt kostet ein Vielfaches der Text-Variante. → **Kostenschätzung vor jedem Lauf** + bewusstes Spend-Budget.
- **Harte API-Limits:** 32 MB / **600 Seiten** pro Request → deshalb die Reduktion für große PDFs.
- **Batch:** 256 MB pro Batch → Blockgröße nach kumulierten base64-MB begrenzen.
- **Spend-Limit:** wird steigen → in der Console anheben. (Bereits zweimal angeschlagen; s. Testläufe.)
- **Rate-Limits (ITPM/RPM):** unkritisch — Batch zählt nicht gegen ITPM, unser ITPM ist 10 Mio.

---

## 6. Phasen

- **Phase 0 — Prototyp/Validierung:** natives PDF an Sonnet an 5–10 Prospekten (klein + reduziert-groß), Datenpunkte vs. Text-Weg (Läufe 1–6) vergleichen, echte Kosten messen. *Go/No-Go vor dem großen Umbau.*
- **Phase 1 — Kern:** `pdf_native.py` (document-Block, Metadaten, Seiten-Reduktion), Routing.
- **Phase 2 — Pfade:** `batch_analysis` + Sync-Worker auf natives PDF; 256-MB-Blockgrenze.
- **Phase 3 — GUI:** Umschalter, Modell-Dropdown, Trim-Fenster-Umbau, Kostenanzeige.
- **Phase 4 — Cleanup:** Text-Pfad/Artefakte/OCR/Haiku entfernen; untracked Debug-Skripte im Repo-Root aufräumen.
- **Phase 5 — Docs:** Patchnotes + Auswertung aktualisieren.

---

## 7. Offene Punkte / Risiken (Entscheidung nötig)

1. **Provider vs. natives PDF (größtes Risiko):** „Provider behalten" + „natives PDF" kollidiert teilweise — die PDF-Übergabe ist providerspezifisch:
   - **Anthropic:** `document`-Block base64 ✓ (voll unterstützt).
   - **Gemini:** eigenes Format (`inline_data`, PDF) — machbar, aber anderer Code.
   - **OpenRouter:** PDF-Support je Modell unterschiedlich, teils gar nicht.
   → **Vorschlag:** natives PDF für **Anthropic** voll umsetzen, **Gemini** best-effort, **OpenRouter** vom Native-PDF-Pfad ausnehmen (bzw. dort Hinweis „nicht unterstützt"). Bitte bestätigen.
2. **Seiten-Auswahl-Signal beim Reduzieren:** günstig **text-/tabellenbasiert** (Seiten-Level, gratis) vs. teurer **Vision-Pass** (Claude wählt Seiten). Vorschlag: text-/tabellenbasiert mit großzügiger Auswahl; Vision nur als Option. Bitte bestätigen.
3. **Migration bestehender Artefakte:** `.trimmed.txt`/`.extracted.json`/`.tables.json`/`.haiku.bak` löschen oder archivieren?
4. **DB/Ergebnisse:** bestehende Werte behalten (nur neue Läufe nativ), kein Reset — Annahme; bitte bestätigen.

---

## 8. Aufräum-/Löschliste (Repo)

- Untracked Debug-Skripte im Root (`analyze_*.py`, `check_*.py`, `test_*.py`, `direct_analysis_100.py`, `final_*.py`, `run_until_limit.py`, `verify_prompt.py`, `batch_100_pdfs.py`, `reanalyse_new_fields.py`, `gui_batch_analysis.py`) + Logs (`*.log`, `batch_log*.txt`) → entfernen.
- PDF-Nebenartefakte (`.trimmed.txt`, `.extracted.json`, `.tables.json`, `.haiku.bak`) → nach Umbau entfernen.
- Ambige Docs (`IMPLEMENTATION_SUMMARY.md`, `IMPROVED_PROMPT_TEMPLATE.md`, `Backup2_Prompt.txt`) + `src/data/` → vor Löschen klären.
