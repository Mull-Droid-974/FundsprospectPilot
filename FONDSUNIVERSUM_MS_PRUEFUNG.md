# Fondsuniversum-Prüfung über Morningstar — Übersicht

**Erstellt:** 2026-07-03
**Quelle:** ISINs aus `data/output/results.db` (Fondsuniversum), geprüft über die **Morningstar-MCP-Schnittstelle** (`mcp.morningstar.com`).
**Detaildaten:** `data/output/fondsuniversum_ms_pruefung.csv` (pro ISIN, Semikolon-getrennt, Excel-tauglich)

---

## Fragestellung

Wie viele ISINs des Universums sind **aktive Klassen mit Vertriebsland Schweiz** — und wie viele fallen raus?

## Kriterien (Morningstar-Datapoints)

| Kriterium | Datapoint | Bedingung |
|---|---|---|
| Aktive Klasse | `OS999` (Status) | == „Active" |
| Vertriebsland Schweiz | `OS018` (Country Registered for Sale) | Liste enthält „Switzerland" |

Ablauf pro ISIN: `id-lookup` (ISIN → Morningstar-Fonds-ID) → `data-tool` (OS999, OS018).

---

## Ergebnis

| | Anzahl | Anteil |
|---|---:|---:|
| **Universum gesamt** | **24.726** | 100,0 % |
| ✅ **Behalten** (aktiv **und** CH-Vertrieb) | **23.401** | **94,6 %** |
| ❌ **Rausgefallen** | **1.325** | **5,4 %** |

### Aufschlüsselung der Rausgefallenen (1.325)

| Grund | Anzahl | Kategorie in CSV |
|---|---:|---|
| Inaktiv **und** kein CH-Vertrieb (aufgelöst/geschlossen) | 694 | `inaktiv_und_kein_CH` |
| Kein Morningstar-Treffer (ISIN unbekannt/veraltet) | 597 | `kein_MS_treffer` |
| Kein CH-Vertrieb (aber aktiv) | 25 | `kein_CH_vertrieb` |
| Inaktiv (aber CH-registriert) | 9 | `inaktiv` |

---

## Einordnung

- **~95 % des Universums bleiben** — plausibel für einen CH-fokussierten Fondsbestand.
- Der größte Ausfall sind **aufgelöste/geschlossene Klassen** (694), die weder aktiv noch in der Schweiz vertriebszugelassen sind.
- **597 ISINs sind Morningstar unbekannt** — vermutlich sehr alte/obsolete oder Nicht-Fonds-ISINs. Diese fallen faktisch ebenfalls raus (Aktiv-/CH-Status nicht bestätigbar) und sind Kandidaten für **Datenpflege** im Universum.

## Methodische Hinweise / Einschränkungen

1. **Status wird über die Fonds-ID (F-…) abgefragt** → fonds-, nicht strikt anteilsklassen-genau. Für die Übersicht solide; eine anteilsklassen-scharfe Aktiv-Prüfung (Klassen-ID pro ISIN) wäre eine Verfeinerung.
2. `OS018` ist auf **Fonds-Ebene** gespeichert (gilt für alle Klassen eines Fonds).
3. Token: `MORNINGSTAR_ACCESS_TOKEN` (Direct-MCP), Prüfung am 2026-07-03.

## Spalten der CSV (`fondsuniversum_ms_pruefung.csv`)

`ISIN` · `Fondsname` · `MS_Treffer` (ja/nein) · `Status` · `Aktiv` (ja/nein) · `Vertrieb_Schweiz` (ja/nein) · `Anzahl_Vertriebslaender` · `Kategorie` · `Behalten` (ja/nein)

Sortierung: Rausgefallene zuerst (nach Kategorie), dann die behaltenen.
