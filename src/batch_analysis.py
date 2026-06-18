"""
Anthropic Message-Batches-Analyse — 50% günstiger als synchrone Calls.

Ablauf:
1. submit_batch(): Preprocessing (synchron, Haiku) je Gruppe, dann ein Batch
   mit einem Request pro Subfonds-Gruppe einreichen. Mapping wird lokal abgelegt.
2. poll_batch(): Status abfragen.
3. fetch_and_store(): Ergebnisse abholen, parsen, taxonomie-validiert in DB speichern.

Nur für Anbieter "anthropic". Ergebnisse kommen asynchron (meist Minuten, max. 24h).
"""

import json
import os
from datetime import datetime
from pathlib import Path

import anthropic

import results_store
from llm_analysis_worker import (
    build_analysis_messages, parse_llm_json, prepare_pdf_text,
    build_wert_maps, match_and_save,
)

_BATCH_DIR = Path(__file__).parent.parent / "data" / "output" / "batches"
_MAX_TOKENS = 16384


def _mapping_path(batch_id: str) -> Path:
    return _BATCH_DIR / f"{batch_id}.json"


def submit_batch(groups: dict, prompt_template: str, model: str, api_key: str,
                 trim_model: str = "claude-haiku-4-5-20251001",
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

    def _flush() -> None:
        """Reicht die gesammelten Requests als einen Batch ein und speichert das Mapping."""
        nonlocal requests, mapping
        if not requests:
            return
        batch = client.messages.batches.create(requests=requests)
        _mapping_path(batch.id).write_text(json.dumps({
            "batch_id": batch.id,
            "model": model,
            "created": datetime.now().isoformat(timespec="seconds"),
            "n_requests": len(requests),
            "items": mapping,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        batch_ids.append(batch.id)
        if progress:
            progress(total, total, f"✓ Block eingereicht: {batch.id} ({len(requests)} Req)")
        requests = []
        mapping = {}

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

        pdf_text = prepare_pdf_text(pdf_path, trim_model=trim_model,
                                    api_key=api_key, provider=provider)
        if not pdf_text:
            if progress:
                progress(idx, total, f"übersprungen (kein Text): {group_key}")
            continue

        system_prompt, user_text = build_analysis_messages(prompt_template, pdf_text, rows)
        custom_id = f"g{idx}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": _MAX_TOKENS,
                "system": [{"type": "text", "text": system_prompt,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user_text}],
            },
        })
        # Nur das speichern, was match_and_save später braucht
        mapping[custom_id] = {
            "group_key": group_key,
            "rows": [{"isin": r["isin"], "anteilsklasse": r.get("anteilsklasse", "")}
                     for r in rows],
        }

        # Block voll → sofort einreichen
        if len(requests) >= chunk_size:
            _flush()

    _flush()  # Rest einreichen

    if not batch_ids:
        raise RuntimeError("Keine analysierbaren Gruppen (kein PDF/Text gefunden).")

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
    Gibt {saved, errored, skipped} zurück."""
    mp = _mapping_path(batch_id)
    if not mp.exists():
        raise RuntimeError(f"Kein lokales Mapping für Batch {batch_id} gefunden.")
    data = json.loads(mp.read_text(encoding="utf-8"))
    model = data.get("model", "")
    items = data.get("items", {})

    client = anthropic.Anthropic(api_key=api_key)
    wert_maps = build_wert_maps()

    saved = errored = skipped = 0
    for entry in client.messages.batches.results(batch_id):
        cid = entry.custom_id
        info = items.get(cid)
        if not info:
            skipped += 1
            continue

        result = entry.result
        if getattr(result, "type", "") != "succeeded":
            errored += 1
            if progress:
                progress(f"✗ {info['group_key']}: {getattr(result, 'type', 'fehler')}")
            continue

        raw = "".join(b.text for b in result.message.content if hasattr(b, "text"))
        parsed = parse_llm_json(raw)
        if not parsed:
            errored += 1
            continue

        try:
            match_and_save(parsed, info["rows"], model, wert_maps)
            saved += len(info["rows"])
            if progress:
                progress(f"✓ {info['group_key']} ({len(info['rows'])} ISIN)")
        except Exception as e:
            errored += 1
            if progress:
                progress(f"✗ {info['group_key']}: {e}")

    return {"saved": saved, "errored": errored, "skipped": skipped}


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
    agg = {"saved": 0, "errored": 0, "skipped": 0, "ended": 0, "pending": 0}

    for b in batches:
        bid = b["batch_id"]
        done_marker = _BATCH_DIR / f"{bid}.done"
        if done_marker.exists():
            continue  # schon abgeholt

        try:
            status = poll_batch(bid, api_key)
        except Exception as e:
            if progress:
                progress(f"✗ {bid}: Status-Fehler {e}")
            continue

        if status["status"] != "ended":
            agg["pending"] += 1
            if progress:
                progress(f"⏳ {bid}: {status['status']} {status['counts']}")
            continue

        agg["ended"] += 1
        res = fetch_and_store(bid, api_key, progress=progress)
        agg["saved"]   += res["saved"]
        agg["errored"] += res["errored"]
        agg["skipped"] += res["skipped"]
        done_marker.write_text("ok", encoding="utf-8")  # nicht erneut abholen

    return agg
