"""
Anthropic Message-Batches-Analyse — 50% günstiger als synchrone Calls.

Ablauf:
1. submit_batch(): Je Gruppe das NATIVE PDF aufbereiten (ganzes PDF oder auf
   relevante Seiten reduziert, s. pdf_native) und als document-Block in einen
   Batch einreichen — ein Request pro Subfonds-Gruppe. Mapping wird lokal abgelegt.
2. poll_batch(): Status abfragen.
3. fetch_and_store(): Ergebnisse abholen, parsen, taxonomie-validiert in DB speichern.

Nur für Anbieter "anthropic" (natives PDF = Anthropic-document-Block).
Ergebnisse kommen asynchron (meist Minuten, max. 24h).
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import anthropic

import results_store
from collections import defaultdict

from llm_analysis_worker import (
    build_analysis_messages, parse_llm_json,
    build_wert_maps, match_and_save, merge_parsed_parts,
)
from pdf_native import prepare_pdf_documents

_BATCH_DIR = Path(__file__).parent.parent / "data" / "output" / "batches"
_MAX_TOKENS = 16384
# Batch-Create gegen transiente Infra-Fehler (502/503/Timeout) absichern, damit
# ein kurzer Ausfall nicht die gesamte (teure) Vorbereitung verwirft.
_CREATE_MAX_RETRIES = 5
_CREATE_BACKOFF_BASE = 2  # Sekunden, exponentiell
_MAX_BATCH_MB = 200       # Grenze unter dem 256-MB-Batch-Limit (base64 ≈ 1,37× Dateigröße)


def _mapping_path(batch_id: str) -> Path:
    return _BATCH_DIR / f"{batch_id}.json"


def submit_batch(groups: dict, prompt_template: str, model: str, api_key: str,
                 trim_model: str = "",   # ignoriert (natives PDF, kein Text-Trim) — Signatur-Kompat
                 provider: str = "anthropic", progress=None,
                 chunk_size: int = 500) -> list[str]:
    """Reicht alle Gruppen blockweise ein. Gibt Liste der batch_ids zurück.

    Pro Block (chunk_size Gruppen) wird sofort EIN Batch eingereicht und das
    Mapping lokal gespeichert — abbruchsicher und blockweise abholbar.

    groups: {group_key: [row, ...]}
    progress: optional callable(done, total, message)
    """
    if provider.lower() != "anthropic":
        raise RuntimeError("Batch-Analyse ist nur für Anbieter 'anthropic' verfügbar.")

    client = anthropic.Anthropic(api_key=api_key)
    _BATCH_DIR.mkdir(parents=True, exist_ok=True)

    items = list(groups.items())
    total = len(items)
    batch_ids: list[str] = []
    requests: list[dict] = []
    mapping: dict[str, dict] = {}
    cum_mb = 0.0

    def _flush() -> None:
        """Reicht die gesammelten Requests als einen Batch ein und speichert das Mapping."""
        nonlocal requests, mapping, cum_mb
        if not requests:
            return
        # Transiente Infra-Fehler (502/503/Timeout/Verbindung) mit Backoff wiederholen.
        # 4xx (z.B. erreichtes Spend-Limit) ist NICHT transient → sofort durchreichen.
        batch = None
        last_exc = None
        for attempt in range(1, _CREATE_MAX_RETRIES + 1):
            try:
                batch = client.messages.batches.create(requests=requests)
                break
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                last_exc = exc
            except anthropic.APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                last_exc = exc
            if attempt < _CREATE_MAX_RETRIES:
                wait = min(_CREATE_BACKOFF_BASE * 2 ** (attempt - 1), 60)
                if progress:
                    progress(total, total,
                             f"[WARTEN] Batch-Create-Fehler (Versuch {attempt}/{_CREATE_MAX_RETRIES}) "
                             f"— erneut in {wait}s …")
                time.sleep(wait)
        if batch is None:
            raise RuntimeError(
                f"Batch-Create nach {_CREATE_MAX_RETRIES} Versuchen fehlgeschlagen "
                f"({len(requests)} vorbereitete Requests): {last_exc}"
            )
        _mapping_path(batch.id).write_text(json.dumps({
            "batch_id": batch.id,
            "model": model,
            "created": datetime.now().isoformat(timespec="seconds"),
            "n_requests": len(requests),
            "items": mapping,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        batch_ids.append(batch.id)
        if progress:
            progress(total, total, f"[OK] Block eingereicht: {batch.id} ({len(requests)} Req)")
        requests = []
        mapping = {}
        cum_mb = 0.0

    for idx, (group_key, rows) in enumerate(items, 1):
        pdf_path = next(
            (r["prospekt_pfad"] for r in rows
             if r.get("prospekt_pfad") and Path(r["prospekt_pfad"]).exists()),
            None,
        )
        if not pdf_path:
            if progress:
                progress(idx, total, f"übersprungen (kein PDF): {group_key}")
            continue

        if progress:
            progress(idx, total, f"Vorbereitung {idx}/{total}: {Path(pdf_path).name}")

        parts = prepare_pdf_documents(pdf_path)
        if not parts:
            if progress:
                progress(idx, total, f"übersprungen (PDF nicht aufbereitbar): {group_key}")
            continue

        n = parts[0]["n_parts"]
        base = f"g{idx}"
        rows_slim = [{"isin": r["isin"], "anteilsklasse": r.get("anteilsklasse", "")}
                     for r in rows]
        for part in parts:
            cid = base if n == 1 else f"{base}p{part['part']}"
            system_prompt, user_content = build_analysis_messages(prompt_template, part["block"], rows)
            requests.append({
                "custom_id": cid,
                "params": {
                    "model": model,
                    "max_tokens": _MAX_TOKENS,
                    "system": [{"type": "text", "text": system_prompt,
                                "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user", "content": user_content}],
                },
            })
            # Bei Split gehören mehrere custom_ids zur selben Gruppe (n_parts) → Merge beim Abholen
            mapping[cid] = {"group_key": group_key, "rows": rows_slim,
                            "part": part["part"], "n_parts": n}
            cum_mb += part["size_mb"] * 1.37  # base64-Aufschlag

        if progress:
            if n > 1:
                progress(idx, total, f"gesplittet in {n} native Teile: {Path(pdf_path).name} "
                                     f"({parts[0]['pages_in']} S.)")
            elif parts[0]["reduced"]:
                progress(idx, total, f"reduziert: {Path(pdf_path).name} "
                                     f"({parts[0]['pages_in']}→{parts[0]['pages_native']} S.)")

        # Block voll → einreichen (alle Teile einer Gruppe bleiben im selben Batch → Merge)
        if len(requests) >= chunk_size or cum_mb >= _MAX_BATCH_MB:
            _flush()

    _flush()  # Rest einreichen

    if not batch_ids:
        raise RuntimeError("Keine analysierbaren Gruppen (kein PDF gefunden/aufbereitbar).")

    return batch_ids


def poll_batch(batch_id: str, api_key: str) -> dict:
    """Gibt {status, counts} zurück. status == 'ended' wenn fertig."""
    client = anthropic.Anthropic(api_key=api_key)
    batch = client.messages.batches.retrieve(batch_id)
    counts = {}
    try:
        rc = batch.request_counts
        counts = {"processing": rc.processing, "succeeded": rc.succeeded,
                  "errored": rc.errored, "canceled": rc.canceled, "expired": rc.expired}
    except Exception:
        pass
    return {"status": batch.processing_status, "counts": counts}


def fetch_and_store(batch_id: str, api_key: str, progress=None) -> dict:
    """Holt die Batch-Ergebnisse ab und speichert sie taxonomie-validiert in die DB.

    Split-PDFs: mehrere custom_ids gehören zur selben Gruppe (n_parts) und werden zu
    EINEM Ergebnis gemerged, bevor gespeichert wird. Gruppen mit fehlenden/fehlerhaften
    Teilen werden gemeldet (kein stiller Skip).
    Gibt {saved, errored, skipped, incomplete} zurück (saved/errored in ISIN)."""
    mp = _mapping_path(batch_id)
    if not mp.exists():
        raise RuntimeError(f"Kein lokales Mapping für Batch {batch_id} gefunden.")
    data = json.loads(mp.read_text(encoding="utf-8"))
    model = data.get("model", "")
    items = data.get("items", {})

    client = anthropic.Anthropic(api_key=api_key)
    wert_maps = build_wert_maps()

    # 1) Ergebnisse pro Gruppe einsammeln (über alle Teile)
    groups: dict = defaultdict(lambda: {"rows": None, "parts": [], "failed": 0, "n_parts": 1})
    skipped = 0
    for entry in client.messages.batches.results(batch_id):
        info = items.get(entry.custom_id)
        if not info:
            skipped += 1
            continue
        g = groups[info["group_key"]]
        g["rows"] = info["rows"]
        g["n_parts"] = info.get("n_parts", 1)
        result = entry.result
        if getattr(result, "type", "") != "succeeded":
            g["failed"] += 1
            continue
        raw = "".join(b.text for b in result.message.content if hasattr(b, "text"))
        parsed = parse_llm_json(raw)
        if parsed:
            g["parts"].append(parsed)
        else:
            g["failed"] += 1

    # 2) pro Gruppe mergen + speichern
    saved = errored = incomplete = 0
    for gkey, g in groups.items():
        if g["rows"] is None:
            continue
        if not g["parts"]:
            errored += len(g["rows"])
            if progress:
                progress(f"[FEHLER] {gkey}: keine verwertbare Antwort "
                         f"({g['failed']}/{g['n_parts']} Teile fehlten)")
            continue
        if g["failed"]:
            incomplete += 1
            if progress:
                progress(f"[TEILWEISE] {gkey}: {g['failed']}/{g['n_parts']} Teile fehlten "
                         f"— mit Rest gespeichert")
        merged = merge_parsed_parts(g["parts"])
        try:
            match_and_save(merged, g["rows"], model, wert_maps)
            saved += len(g["rows"])
            if progress and not g["failed"]:
                progress(f"[OK] {gkey} ({len(g['rows'])} ISIN)")
        except Exception as e:
            errored += len(g["rows"])
            if progress:
                progress(f"[FEHLER] {gkey}: {e}")

    return {"saved": saved, "errored": errored, "skipped": skipped, "incomplete": incomplete}


def list_local_batches() -> list[dict]:
    """Lokal gespeicherte Batches (für 'Ergebnisse abholen'-Auswahl)."""
    if not _BATCH_DIR.exists():
        return []
    out = []
    for f in sorted(_BATCH_DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"batch_id": d.get("batch_id", f.stem),
                        "created": d.get("created", ""),
                        "n_requests": d.get("n_requests", 0),
                        "model": d.get("model", "")})
        except Exception:
            continue
    return out


def fetch_all_pending(api_key: str, progress=None) -> dict:
    """Prüft alle lokal gespeicherten Batches und holt die fertigen ('ended') ab.
    Bereits abgeholte (Marker-Datei .done) werden übersprungen.
    Gibt {saved, errored, skipped, ended, pending} zurück."""
    batches = list_local_batches()
    agg = {"saved": 0, "errored": 0, "skipped": 0, "incomplete": 0, "ended": 0, "pending": 0}

    for b in batches:
        bid = b["batch_id"]
        done_marker = _BATCH_DIR / f"{bid}.done"
        if done_marker.exists():
            continue  # schon abgeholt

        try:
            status = poll_batch(bid, api_key)
        except Exception as e:
            if progress:
                progress(f"[FEHLER] {bid}: Status-Fehler {e}")
            continue

        if status["status"] != "ended":
            agg["pending"] += 1
            if progress:
                progress(f"[WARTEN] {bid}: {status['status']} {status['counts']}")
            continue

        agg["ended"] += 1
        res = fetch_and_store(bid, api_key, progress=progress)
        agg["saved"]      += res["saved"]
        agg["errored"]    += res["errored"]
        agg["skipped"]    += res["skipped"]
        agg["incomplete"] += res.get("incomplete", 0)
        done_marker.write_text("ok", encoding="utf-8")  # nicht erneut abholen

    return agg
