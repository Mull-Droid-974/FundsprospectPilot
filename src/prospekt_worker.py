"""
Prospekt-Download-Worker — 2-Phasen-Pipeline.

Phase 1: Metadaten laden (subfonds_id, subfonds_name, prospekt_url etc.)
         Für alle ISINs ohne subfonds_id — ein API-Call pro ISIN, kein Download.

Phase 2: Gruppierter Download (1 PDF pro Unterfonds)
         ISINs werden nach subfonds_id gruppiert.
         Pro Gruppe wird genau ein Prospekt heruntergeladen;
         alle ISINs der Gruppe verweisen auf dieselbe Datei.
"""

import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import fundinfo_client
import results_store


@dataclass
class ProspektEvent:
    type: str       # "log" | "progress" | "done" | "error"
    isin: str = ""
    message: str = ""
    phase: int = 1          # 1 = Metadaten, 2 = Download
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0


class ProspektWorker(threading.Thread):
    def __init__(
        self,
        isins: list[dict],          # Zeilen aus results_store (einzelne ISIN oder alle)
        pdf_folder: Path,
        event_queue: queue.Queue,
        delay: float = 1.5,
        single_mode: bool = False,  # True = nur diese ISINs, keine Gruppen-Erweiterung
    ):
        super().__init__(daemon=True)
        self._isins = isins
        self._pdf_folder = pdf_folder
        self._queue = event_queue
        self._delay = delay
        self._single_mode = single_mode
        self._stop_flag = False
        self._done = 0
        self._skipped = 0
        self._failed = 0
        self._phase = 1

    def stop(self):
        self._stop_flag = True

    def _emit(self, type_: str, isin: str = "", message: str = "", total: int = 0):
        self._queue.put(ProspektEvent(
            type=type_,
            isin=isin,
            message=message,
            phase=self._phase,
            total=total or len(self._isins),
            done=self._done,
            skipped=self._skipped,
            failed=self._failed,
        ))

    # ─── Phase 1: Metadaten ──────────────────────────────────────────────────

    def _load_metadata(self, isins_without_meta: list[dict]):
        self._phase = 1
        total = len(isins_without_meta)
        self._emit("log", message=f"Phase 1: Metadaten für {total} ISIN(s) laden …", total=total)

        for row in isins_without_meta:
            if self._stop_flag:
                return
            isin = row["isin"]
            self._emit("log", isin, f"Metadaten abrufen …", total=total)

            meta = None
            try:
                meta = fundinfo_client.fetch_fund_metadata(isin, delay=self._delay)
            except Exception as exc:
                self._emit("error", isin, f"Metadaten-Fehler: {exc}", total=total)
                self._failed += 1
                continue

            if meta:
                try:
                    results_store.update_fundinfo_meta(
                        isin,
                        subfonds_id=meta["subfonds_id"],
                        subfonds_name=meta["subfonds_name"],
                        umbrella_id=meta["umbrella_id"],
                        anteilsklasse=meta["anteilsklasse"],
                        ausschuettungsart=meta["ausschuettungsart"],
                        fondswaehrung=meta["fondswaehrung"],
                        fundinfo_ter=meta["fundinfo_ter"],
                        prospekt_url=meta["prospekt_url"],
                        fundinfo_investor_type=meta.get("fundinfo_investor_type", ""),
                        ongoing_charges_datum=meta.get("ongoing_charges_datum", ""),
                        qualif_anleger_ch=meta.get("qualif_anleger_ch", ""),
                        institutional_ch=meta.get("institutional_ch", ""),
                    )
                except Exception as exc:
                    self._emit("error", isin, f"DB-Fehler Phase 1: {exc}", total=total)
                    self._failed += 1
                    continue
                self._done += 1
                self._emit("progress", isin,
                    f"Unterfonds: {meta['subfonds_name'] or '—'}", total=total)
            else:
                self._failed += 1
                self._emit("error", isin, "Keine Daten von fundinfo", total=total)
                results_store.mark_meta_not_found(isin)

    # ─── Phase 2: Gruppierter Download ───────────────────────────────────────

    def _download_groups(self, target_isins: set[str]):
        self._phase = 2
        self._done = 0
        self._skipped = 0
        self._failed = 0

        groups = results_store.get_subfonds_groups()

        # Im single_mode nur Gruppen verarbeiten, die target ISINs enthalten
        if self._single_mode:
            groups = {
                k: v for k, v in groups.items()
                if any(r["isin"] in target_isins for r in v)
            }

        # Gruppen ohne subfonds_id (leerer Key) einzeln behandeln
        ungrouped = groups.pop("", [])
        if ungrouped:
            groups.update({f"__single_{r['isin']}": [r] for r in ungrouped
                           if r["isin"] in target_isins})

        total_groups = len(groups)
        self._emit("log", message=f"Phase 2: {total_groups} Unterfonds-Gruppen …",
                   total=total_groups)

        for group_key, group_rows in groups.items():
            if self._stop_flag:
                self._emit("log", message="Download abgebrochen.")
                break

            isins_in_group = [r["isin"] for r in group_rows]
            needs_download = [
                r for r in group_rows
                if not r.get("prospekt_pfad") or not Path(r["prospekt_pfad"]).exists()
            ]

            if not needs_download:
                self._skipped += 1
                self._emit("log", isin=isins_in_group[0],
                    message=f"Gruppe übersprungen (alle {len(group_rows)} ISINs vorhanden)",
                    total=total_groups)
                continue

            # A) Bereits vorhandener Pfad in der Gruppe?
            existing_path = next(
                (r["prospekt_pfad"] for r in group_rows
                 if r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists()),
                None
            )
            if existing_path:
                for r in needs_download:
                    results_store.update_prospekt(r["isin"], existing_path,
                                                  r.get("prospekt_url", ""))
                self._done += 1
                self._emit("progress", isin=isins_in_group[0],
                    message=f"Verknüpft ({len(needs_download)} ISINs → {Path(existing_path).name})",
                    total=total_groups)
                continue

            # B) URL aus DB oder neu ermitteln
            ref_row = group_rows[0]
            prospekt_url = next(
                (r.get("prospekt_url", "") for r in group_rows
                 if r.get("prospekt_url") and not r["prospekt_url"].startswith("__")),
                ""
            )
            if not prospekt_url:
                self._emit("log", isin=ref_row["isin"], message="URL ermitteln …",
                           total=total_groups)
                try:
                    doc_info = fundinfo_client.discover_prospectus_url(
                        ref_row["isin"], delay=self._delay)
                    prospekt_url = doc_info["url"] if doc_info else ""
                except Exception as exc:
                    self._failed += 1
                    self._emit("error", isin=ref_row["isin"],
                               message=f"URL-Fehler: {exc}", total=total_groups)
                    continue

            if not prospekt_url:
                self._failed += 1
                self._emit("error", isin=ref_row["isin"],
                           message="Kein Prospekt auf fundinfo gefunden", total=total_groups)
                continue

            # Prüfen ob diese URL bereits von einer anderen Gruppe vorliegt
            cached = results_store.get_by_prospekt_url(prospekt_url)
            if cached and cached.get("prospekt_pfad") and Path(cached["prospekt_pfad"]).exists():
                cached_path = cached["prospekt_pfad"]
                for r in group_rows:
                    results_store.update_prospekt(r["isin"], cached_path, prospekt_url)
                self._done += 1
                self._emit("progress", isin=ref_row["isin"],
                    message=f"Verknüpft aus Cache → {Path(cached_path).name}",
                    total=total_groups)
                continue

            # C) Download
            subfonds_code = ref_row.get("subfonds_id", "")[:8] if not (
                next((r.get("subfonds_name") for r in group_rows if r.get("subfonds_name")), "")
            ) else ""
            # Verwende subfonds_code aus Metadaten falls in prospekt_url kodiert,
            # ansonsten ersten 8 Zeichen der subfonds_id als Fallback
            lang = next(
                (r.get("prospekt_lang", "") for r in group_rows if r.get("prospekt_lang", "")),
                "XX"
            )
            # Extrahiere Sprache aus URL falls vorhanden
            if not lang or lang == "XX":
                import re
                m = re.search(r'_([A-Z]{2})_\d{4}', prospekt_url)
                if m:
                    lang = m.group(1)

            # Kurznamen für Datei aus subfonds_name ableiten
            subfonds_name = next(
                (r.get("subfonds_name", "") for r in group_rows if r.get("subfonds_name")), "")
            # Nimm letzten Teil nach " - " als Kurzname, oder ersten 20 Zeichen
            short_name = subfonds_name.split(" - ")[-1][:30] if subfonds_name else group_key[:8]

            pdf_path = None
            try:
                pdf_path = fundinfo_client.download_prospekt_from_url(
                    prospekt_url, short_name, lang, str(self._pdf_folder)
                )
            except Exception as exc:
                self._failed += 1
                self._emit("error", isin=ref_row["isin"],
                           message=f"Download-Fehler: {exc}", total=total_groups)
                continue

            if pdf_path:
                for r in group_rows:
                    results_store.update_prospekt(r["isin"], pdf_path, prospekt_url)
                self._done += 1
                self._emit("progress", isin=ref_row["isin"],
                    message=f"{Path(pdf_path).name} → {len(group_rows)} ISIN(s)",
                    total=total_groups)
            else:
                self._failed += 1
                self._emit("error", isin=ref_row["isin"],
                           message="Download fehlgeschlagen", total=total_groups)

            time.sleep(self._delay)

    # ─── Hauptlauf ───────────────────────────────────────────────────────────

    def run(self):
        try:
            self._pdf_folder.mkdir(parents=True, exist_ok=True)
            results_store.cleanup_sentinels()
            target_isins = {r["isin"] for r in self._isins}

            # Phase 1: ISINs ohne subfonds_id mit Metadaten befüllen
            # ISINs mit Sentinel (__nf_*) haben nicht-leere subfonds_id → werden korrekt ausgeschlossen
            without_meta = [r for r in self._isins if not r.get("subfonds_id")]
            if without_meta:
                self._load_metadata(without_meta)
                if self._stop_flag:
                    self._emit("done", message="Abgebrochen nach Phase 1.")
                    return
                # Frische Daten aus DB für Phase 2
                self._isins = [
                    results_store.get_result(r["isin"]) or r for r in self._isins
                ]

            # Phase 2: Gruppierter Download
            self._download_groups(target_isins)

            self._emit("done", message=(
                f"Fertig. Neu: {self._done}, Übersprungen: {self._skipped}, Fehler: {self._failed}"
            ))
        except Exception as exc:
            self._emit("error", message=f"Worker-Fehler: {exc}")
            self._emit("done", message=f"Abgebrochen durch unerwarteten Fehler: {exc}")
