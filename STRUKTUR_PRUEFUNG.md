# Struktur-Prüfung — Auffälligkeiten

**Erstellt:** 2026-07-03
**Quelle:** `struktur_store.build_structure()` (fund_results + morningstar_data)
**Detaildaten:** `data/output/struktur_pruefung.csv` (Semikolon-getrennt, Excel-tauglich)

Prüfung der Hierarchie **Umbrella (MS-Branding) → Portfolio (Subfonds) → Anteilsklasse**
auf saubere Identifier und Bezeichnungen.

## Ergebnis (23.401 Klassen)

| Status | Anzahl |
|---|---:|
| 🟢 OK | 21.885 |
| 🟡 Warnung | 1.515 |
| 🔴 Fehler | 1 |
| **Markiert gesamt** | **1.516** |

## Befunde (Mehrfachnennung je Klasse möglich)

| Befund | Anzahl | Bedeutung |
|---|---:|---|
| **Name weicht ab** | 897 | Subfonds-/Prospektname hat keine Wort-Überschneidung mit dem Morningstar-Namen → prüfen (Zuordnung/Schreibweise) |
| **Anteilsklassen-Bezeichnung leer** | 879 | Feld `anteilsklasse` ist leer → aus Prospekt/MS nachtragen |
| **subfonds_id fehlt** | 23 | Portfolio-Zuordnung fehlt (Klasse hängt als Einzel-Knoten) |
| **keine Morningstar-Daten** | 1 | ISIN in Morningstar nicht auflösbar |

## Einordnung

- Die beiden großen Gruppen sind **Namensabweichungen** (897) und **leere Anteilsklassen-Bezeichnungen** (879) — beides Datenpflege, keine strukturellen Fehler.
- „Name weicht ab" ist bewusst streng (keine Wort-Überschneidung); ein Teil davon sind legitime Fälle (z.B. stark abgekürzte/englische MS-Namen). Die CSV zeigt beide Namen zum schnellen Sichten.
- Nur **1 echter Fehler** (ISIN ohne MS-Daten) und **23 fehlende `subfonds_id`** sind strukturell relevant.

## Spalten der CSV

`Umbrella` · `Subfonds` · `ISIN` · `Anteilsklasse` · `Prüfstatus` (🔴/🟡) · `Befunde`

Sortierung: nach Prüfstatus, dann Umbrella/Subfonds.
