# Batch-Analyse — Testläufe Auswertung (Läufe 1–5)

**Erstellt:** 2026-06-25 · **Letzte Aktualisierung:** 2026-07-02
**Zweck:** Vergleich der Batch-Analyse von Fondsprospekten über die Anthropic **Message Batches API** (50% Rabatt) mit verschiedenen Modellkombinationen für **Trimming** (Vorverarbeitung großer PDFs) und **Analyse** (Klassifikation) — von Haiku bis Opus 4.8.

---

## Methodik

- **Quelle:** `data/output/results.db` — 24.726 Einträge / 5.650 Subfonds-Gruppen, 4.531 Gruppen mit vorhandenem PDF.
- **Auswahl pro Lauf:** 50 Subfonds-Gruppen, gleichmäßig über die PDF-Größenverteilung verteilt (echte Größenvarianz, ~0,12–19,96 MB, Median ~3,25 MB). Läufe 1, 2, 3, 5 nutzen jeweils **verschiedene, überschneidungsfreie** PDF-Sätze; **Lauf 4 nutzt exakt den PDF-Satz von Lauf 1** (kontrollierter A/B).
- **Eine Analyse pro Subfonds-Gruppe:** alle Anteilsklassen/ISINs einer Gruppe gehen in EINEN LLM-Call (PDF einmal pro Gruppe).
- **API:** alle Analysen über `messages.batches.create` (Batch-API, asynchron, −50%). Trimming großer PDFs (>150 Seiten / >30 MB) läuft **synchron** (kein Batch-Rabatt).
- **Speicherung:** Ergebnisse taxonomie-validiert in `fund_results` (`update_llm_analysis`).
- **Datenpunkt** = ein gefülltes Klassifikationsfeld (von 7: fondstyp, anlegertyp, kundentyp, llm_segmentierung, mindestanlage, dienstleistung, vertriebskanal).

### Preise (pro 1 Mio. Token)

| Modell | Input (sync) | Output (sync) | Input (Batch −50%) | Output (Batch −50%) |
|---|---:|---:|---:|---:|
| Haiku 4.5 | $1,00 | $5,00 | $0,50 | $2,50 |
| Sonnet 4.6 | $3,00 | $15,00 | $1,50 | $7,50 |
| Opus 4.8 | $5,00 | $25,00 | $2,50 | $12,50 |

Prompt-Caching greift nur auf den (statischen) System-Prompt und erst **ab Sonnet** (Mindest-Prefix: Haiku 4.096 Token > System-Prompt; Sonnet/Opus niedriger). Batch-Cache Sonnet: Read $0,15 / Write $1,875 pro 1M.

---

## Lauf 1 — Haiku (Trim + Analyse) · Baseline

- **Batch-ID:** `msgbatch_01Jq6DYvwohZJWwoaMPrfkeS` · **Modell:** `claude-haiku-4-5-20251001` (Trim + Analyse)
- **PDF-Satz A** · 50 PDFs / **239** ISINs · 50/50 erfolgreich, 0 Fehler · Laufzeit ~47 Min

| Position | Token | USD |
|---|---:|---:|
| Input | 502.898 | $0,2514 |
| Output | 92.068 | $0,2302 |
| Cache-Read / -Write | 0 / 0 | $0,0000 |
| **Gesamt** | **594.966** | **$0,4816** |

Cache = 0 → System-Prompt liegt unter Haikus 4.096-Token-Mindestgröße, Prompt-Caching greift nicht.

---

## Lauf 2 — Haiku-Trim + Sonnet-Analyse · Kompromiss

- **Batch-ID:** `msgbatch_01Uc1XySaS7rDN8WmtCVj3zn` · Trim `claude-haiku-4-5`, Analyse `claude-sonnet-4-6`
- **PDF-Satz B** · 50 PDFs / **225** ISINs · 50/50, 0 Fehler

| Position | Token | USD |
|---|---:|---:|
| Input | 334.573 | $0,5019 |
| Output | 92.121 | $0,6909 |
| Cache-Read | 72.240 | $0,0108 |
| Cache-Write | 99.760 | $0,1870 |
| **Gesamt** | **598.694** | **$1,3907** |

Prompt-Caching greift (Sonnet). **Output ($0,69) > Input ($0,50)** → bei gekapptem Input dominiert der Output die Sonnet-Kosten.

---

## Lauf 3 — Sonnet (Trim + Analyse) · Maximaltest

- **Batch-ID:** `msgbatch_012YeD1KkrQPaPNFd5ALbxuv` · **Modell:** `claude-sonnet-4-6` (Trim + Analyse)
- **PDF-Satz C** · 50 PDFs / **228** ISINs · 50/50, 0 Fehler

| Position | Analyse (Batch) |
|---|---|
| Input / Output | 318.168 / 93.312 |
| Cache-Read / -Write | 13.760 / 158.240 |
| **Analyse-Kosten** | **$1,4759** |

**Trim** (synchron Sonnet, 4 Calls): Output 9.717, Cache-Write 66.968 → **$0,3969**
**Gesamt: $1,8728**

> ⚠️ Trim untertrieben: nur **4 PDFs frisch** getrimmt (Rest aus Cache eines abgebrochenen Vorversuchs).

---

## Lauf 4 — Sonnet (Trim + Analyse) auf IDENTISCHEM Lauf-1-Satz · kontrollierter A/B

- **Batch-ID:** `msgbatch_014tr8aNZbL1bmCEonpMhC1Y` · **Modell:** `claude-sonnet-4-6` (Trim + Analyse)
- **PDF-Satz A (= Lauf 1)** · 50 PDFs / **239** ISINs · 50/50, 0 Fehler
- **Speicherung:** nur bei Abweichung/fehlendem Wert; **175 ISINs geändert**, markiert als `modell = "claude-sonnet-4-6 [Lauf4]"`. Lauf-1-Baseline vorab gesichert (`batch_test_baseline_v4.json`).

| Position | USD |
|---|---:|
| Analyse (Batch) | $1,2866 |
| Trim (synchron Sonnet, 8 frische Trims) | $0,9642 |
| **Gesamt** | **$2,2507** |

> ⚠️ Trim untertrieben: nur **8** frisch getrimmt (26 aus Vorversuch wiederverwendet). Komplett frisch läge der Sonnet-Trim ~3–4× höher.

### A/B Sonnet (Lauf 4) vs. Haiku (Lauf 1) — identische PDFs

| Feld | Lauf 1 (Haiku) | Lauf 4 (Sonnet) | Δ |
|---|---|---|---|
| fondstyp | 239 (100%) | 239 (100%) | 0 |
| llm_segmentierung | 239 (100%) | 239 (100%) | 0 |
| anlegertyp | 174 (73%) | 208 (87%) | **+14 pp** |
| kundentyp | 148 (62%) | 194 (81%) | **+19 pp** |
| vertriebskanal | 89 (37%) | 100 (42%) | +5 pp |
| dienstleistung | 48 (20%) | 51 (21%) | +1 pp |
| mindestanlage | 62 (26%) | 59 (25%) | −1 pp |
| **Datenpunkte** | **999** (Ø 4,18) | **1.090** (Ø 4,56) | **+91 (+9%)** |

**Abweichungen/Füllungen (Sonnet ggü. gespeicherten Haiku-Werten):** 214 Abweichungen, 170 neu gefüllt. Auffällig: **87 Abweichungen bei `llm_segmentierung`** (36% der ISINs) + 10 bei `fondstyp` → Sonnet klassifiziert dort *anders*. Ohne Ground-Truth nicht entscheidbar, welches „richtiger" ist. `mindestanlage` minimal schlechter (59 vs 62) — Sonnet ist nicht überall überlegen.

---

## Lauf 5 — Sonnet-Trim + Opus-4.8-Analyse · neuer PDF-Satz

- **Batch-ID:** `msgbatch_01GgefeaokPcGy86kSLfjThK` · Trim `claude-sonnet-4-6`, Analyse `claude-opus-4-8`
- **PDF-Satz E (neu)** · 50 PDFs / **236** ISINs · 50/50, 0 Fehler
- **Datenpunkte:** 1.001 (Ø 4,24/ISIN)

| Position | Token | USD |
|---|---|---:|
| Analyse (Opus Batch) | in 396.519 / out 108.683 / cr 233.926 / cw 4.774 | $2,4232 |
| Trim (synchron Sonnet, 32 frische Calls) | out 108.607 / cw 656.670 | $4,0919 |
| **Gesamt** | | **$6,5151** |

**Trefferquoten:** fondstyp 100%, segmentierung 100%, anlegertyp 78%, kundentyp 76%, vertriebskanal 35%, dienstleistung 22%, mindestanlage 12%.

> ⚠️ **Teuerster Lauf mit Abstand** (~3× Lauf 4, ~14× Lauf 1): 32 frische Sonnet-Trims (kein Cache) → Trim dominiert ($4,09); Opus-Analyse $2,42. Dies zeigt zugleich die **realen** Sonnet-Trim-Kosten bei einem komplett frischen Satz.
> ⚠️ **Nicht kontrolliert:** neuer PDF-Satz → nicht direkt mit den Sonnet-Läufen vergleichbar. Auffällig trotzdem: Ø 4,24 Datenpunkte/ISIN **unter** den Sonnet-Läufen (4,56–4,65), `anlegertyp`/`kundentyp` nicht besser → **kein sichtbarer Opus-Vorteil** bei viel höheren Kosten.

---

## Lauf 6 — Sonnet-Batch, VOLLTEXT ohne Trim (20 gemischte PDFs)

- **Batch-ID:** `msgbatch_011MzmVs8SBPdyY2UL2S2Uv9` · **Modell:** `claude-sonnet-4-6` · **kein Trim** (kompletter extrahierter Text direkt in die Analyse)
- **PDF-Satz F (neu)** · **20** PDFs gemischter Größe / **116** ISINs · 20/20, 0 Fehler
- **Datenpunkte:** 500 (Ø 4,31/ISIN)

| Position | Token | USD |
|---|---|---:|
| Analyse (Sonnet Batch, Volltext) | in 3.480.716 / out 44.750 / cr 13.760 / cw 55.040 | **$5,6620** |

**Trefferquoten:** fondstyp 100%, segmentierung 100%, kundentyp 88%, anlegertyp 81%, vertriebskanal 28%, mindestanlage 20%, dienstleistung 15%.

> ⚠️ **Nur 20 PDFs** → Absolutwerte nicht mit den 50er-Läufen vergleichbar; **$/PDF** und **$/Datenpunkt** sind die fairen Metriken.
> **Zentrale Erkenntnis:** Volltext ohne Trim kostet **~$0,28/PDF** (3,48 Mio. Input-Token für 20 PDFs) — **~10× teurer pro PDF** als der getrimmte Lauf 2 (~$0,028/PDF), **ohne** erkennbaren Qualitätsvorteil. Und es ist **weiterhin Text** → Bilder/Grafiken bleiben verloren. Bestätigt: „Volltext rein" ist der teure *und* weiter verlustbehaftete Weg → der Ausweg ist **natives PDF** (siehe `REFACTOR_PLAN_native_pdf.md`), nicht Volltext-Text.

---

## Gesamtübersicht — alle sechs Läufe

> Läufe 1, 2, 3, 5, 6 nutzen **verschiedene** PDF-Sätze (Absolutwerte nur indikativ; Lauf 6 zudem nur 20 PDFs). **Lauf 4 vs. Lauf 1** ist der einzige streng kontrollierte A/B-Vergleich (identische PDFs).

| | Lauf 1 | Lauf 2 | Lauf 3 | Lauf 4 | Lauf 5 | Lauf 6 |
|---|---|---|---|---|---|---|
| **Was war der Lauf?** | Baseline | Kompromiss | Maximaltest | Kontroll. A/B | Opus-Test | Volltext o. Trim |
| Trim-Modell | Haiku | Haiku | Sonnet | Sonnet | Sonnet | **keiner** |
| Analyse-Modell | Haiku | Sonnet | Sonnet | Sonnet | **Opus 4.8** | Sonnet |
| PDF-Satz | A | B | C | **A (=L1)** | E | F |
| PDFs / ISINs | 50 / 239 | 50 / 225 | 50 / 228 | 50 / 239 | 50 / 236 | **20 / 116** |
| Datenpunkte | 999 | 984 | 1.060 | 1.090 | 1.001 | 500 |
| Ø Datenpunkte/ISIN | 4,18 | 4,37 | 4,65 | 4,56 | 4,24 | 4,31 |
| Kosten Analyse | $0,48 | $1,39 | $1,48 | $1,29 | $2,42 | $5,66 |
| Kosten Trim | ~0 | ~0 | $0,40¹ | $0,96¹ | $4,09 | – |
| **Kosten gesamt** | **$0,48** | **$1,39** | **$1,87** | **$2,25** | **$6,52** | **$5,66** |
| $ / PDF | $0,010 | $0,028 | $0,037 | $0,045 | $0,130 | **$0,283** |
| $ / Datenpunkt | $0,0005 | $0,0014 | $0,0018 | $0,0021 | $0,0065 | **$0,0113** |

¹ Trim in Läufen 3+4 untertrieben (nur 4 bzw. 8 frische Trims; Rest gecacht). Lauf 5 (32 frische Trims) zeigt die realen Sonnet-Trim-Kosten.

### Trefferquoten pro Feld (alle Läufe)

| Feld | Lauf 1 | Lauf 2 | Lauf 3 | Lauf 4 | Lauf 5 | Lauf 6 |
|---|---|---|---|---|---|---|
| fondstyp | 100% | 100% | 100% | 100% | 100% | 100% |
| llm_segmentierung | 100% | 100% | 100% | 100% | 100% | 100% |
| anlegertyp | 73% | 80% | 87% | 87% | 78% | 81% |
| kundentyp | 62% | 78% | 84% | 81% | 76% | 88% |
| vertriebskanal | 37% | 35% | 38% | 42% | 35% | 28% |
| mindestanlage | 26% | 28% | 35% | 25% | 12% | 20% |
| dienstleistung | 20% | 17% | 20% | 21% | 22% | 15% |

---

## Limits (zur Einordnung)

- **Batch-API:** eigener Limit-Topf (RPM auf Erstell-Calls + Queue 100k–500k Req je Tier + 100k Req/Batch). Tokens zählen **nicht** gegen ITPM/OTPM. Der gesamte Korpus (~4.500 Req) passt in einen Batch auf jedem Tier → unkritisch.
- **Synchroner Trim:** zählt gegen **ITPM** (Sonnet Tier 1 = 30K). Ein voller Trim-Chunk (~25K Token) sprengt Tier 1 fast allein → 429-Quelle. Daher: Trim minimieren / batchen / auf Haiku.
- **Spend-Limit:** Die Sonnet-Trim-lastigen Läufe sind zweimal ins monatliche Spend-Limit gelaufen (Trimming ist der Kostentreiber, nicht die Batch-Analyse).

---

## Fazit

1. **Sonnet-Analyse lohnt sich** — der kontrollierte Lauf 4 belegt auf identischen PDFs klar bessere Ausbeute bei `anlegertyp` (+14 pp) und `kundentyp` (+19 pp), den im Fließtext „versteckten" Feldern. Das ist der eigentliche Mehrwert.
2. **Sonnet-Trim lohnt sich NICHT** — Wechsel des *Trim*-Modells von Haiku (Lauf 2) auf Sonnet (Lauf 4) bringt kaum Mehrausbeute, kostet aber erheblich und ist der Rate-Limit-/Spend-Limit-Treiber. Trimmen ist reine Textreduktion — dafür reicht Haiku.
3. **Opus 4.8 lohnt sich NICHT für diese Aufgabe** (Lauf 5) — teuerster Lauf ($6,52, ~3× Lauf 4), aber **kein sichtbarer Qualitätsvorteil** (Ø 4,24 unter den Sonnet-Läufen; anlegertyp/kundentyp nicht besser). Einschränkung: neuer Satz, nicht streng kontrolliert — für ein Endurteil bräuchte es Opus auf identischem Satz (analog Lauf 4); die Kosten sprechen aber klar dagegen.
4. **Empfehlung: „Lauf-2-Kombi" = Haiku-Trim (nur wo nötig) + Sonnet-Analyse.** Beste Qualität/Kosten-Balance, höheres ITPM-Limit beim Trimmen, voll batchfähig. Opus reserviert für Fälle, wo Sonnet nachweislich scheitert.
5. **Trim nur für wirklich große PDFs**, getriggert nach **gefilterter Tokenzahl** statt Seiten/MB (`_MAX_PAGES`). Kleine/mittlere PDFs direkt in die Batch-Analyse. Lauf 5 zeigt die realen Trim-Kosten: **$4,09 für 32 frische Sonnet-Trims** — der größte Vermeidungshebel.
6. **`fondstyp`/`segmentierung`:** alle Modelle 100% — hier reicht Haiku. Aber 36% Sonnet-Abweichung bei `segmentierung` → Genauigkeit stichprobenartig gegen Referenz prüfen.
7. **Output verschlanken:** Output dominiert die Kosten (Lauf 2: $0,69 Output vs $0,50 Input; bei Opus noch stärker). Kompaktes JSON / kürzere `begründung` / optionale `*_roh` senken den größten Einzelposten.
8. **Hochrechnung Gesamtkorpus** (~4.531 Gruppen, nur Analyse): Lauf-2-Kombi grob **~$125** (× $1,39/50). Opus-Weg wäre um ein Vielfaches teurer (~$220 Analyse + ~$370 frischer Sonnet-Trim). Batch-Limits auf jedem Tier unkritisch; einziger Engpass wäre synchrones Sonnet-Trimming (ITPM) → Trim minimieren/auf Haiku/batchen.
9. **Lauf 6 (Volltext ohne Trim):** bestätigt, dass „ganzes PDF als Text rein" der **teuerste** Weg pro PDF ist (**~$0,28/PDF, ~10× Lauf 2**) **ohne** Qualitätsgewinn — und weiterhin **Text**, also Bilder/Grafiken/komplexe Tabellen bleiben verloren.

> **Neue Richtung (überholt Empfehlungen 2–4):** Nach diesen Läufen ist die Entscheidung gefallen, auf **natives PDF (Vision)** umzubauen — Analyse bekommt die Seite visuell (kein Extraktionsverlust), große PDFs werden auf relevante Seiten reduziert, **kein Haiku mehr**. Details: **`docs/REFACTOR_PLAN_native_pdf.md`**. Die obigen Haiku-Trim-Empfehlungen sind damit historischer Testbefund, nicht mehr die Zielarchitektur.

---

## Anhang — Rohdaten & Artefakte

- Batch-Mappings: `data/output/batches/<batch-id>.json`
- Report-JSONs (Scratchpad): `batch_test_report_v{2..5}.json`, Lauf-4-Baseline `batch_test_baseline_v4.json`
- DB-Markierung Lauf 4: `SELECT * FROM fund_results WHERE modell LIKE '%[Lauf4]%'` (175 ISINs)
- Modelle je Lauf in `fund_results.modell`: Lauf 1 `claude-haiku-4-5-20251001`; Läufe 2–4 `claude-sonnet-4-6` (Lauf 4 zusätzl. `[Lauf4]`); Lauf 5 `claude-opus-4-8`
