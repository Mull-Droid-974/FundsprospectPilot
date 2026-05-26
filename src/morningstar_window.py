"""
Morningstar Datenanreicherung — UI-Fenster.

Drei Tabs:
  Übersicht  — Treeview aller ISINs mit MS-Status + Batch-Steuerung
  Attribute  — Auswahl welche Morningstar-Felder abgerufen werden sollen
  Log        — Detaillierter API-Verlauf
"""

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv, set_key

import morningstar_client
import morningstar_store

load_dotenv()

# ─── Farben ──────────────────────────────────────────────────────────────────
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
ACCENT_GOLD     = "#f9e2af"
BTN_BG          = "#45475a"
BTN_ACTIVE      = "#585b70"

_TREE_COLS = [
    ("isin",       "ISIN",          130),
    ("ms_category","Kategorie",     220),
    ("ms_rating",  "Rating",         70),
    ("ms_investor_type", "Anlegertyp", 120),
    ("ms_legal_type",    "Rechtsform",  90),
    ("ms_fetch_date",    "Abgerufen",  130),
    ("status",     "Status",         100),
]


# ─── Worker ──────────────────────────────────────────────────────────────────

class MorningstarWorker(threading.Thread):
    """Ruft Morningstar-Daten für eine Liste von ISINs via MCP ab."""

    def __init__(
        self,
        isins: list[str],
        field_keys: list[str],
        token: str,
        result_queue: queue.Queue,
    ):
        super().__init__(daemon=True)
        self._isins      = isins
        self._field_keys = field_keys
        self._token      = token
        self._queue      = result_queue
        self._stop_flag  = False
        self._done       = 0
        self._failed     = 0

    def stop(self):
        self._stop_flag = True

    def _emit(self, evt: str, payload=None):
        self._queue.put((evt, payload))

    def run(self):
        total = len(self._isins)
        try:
            self._emit("log", f"Starte MCP-Abruf für {total} ISINs …")
            self._emit("log", f"Endpoint: {morningstar_client.MCP_ENDPOINT}")

            # MCP-Tools entdecken (einmalig, für Log-Ausgabe)
            try:
                tools = morningstar_client.list_mcp_tools(self._token)
                names = [t["name"] for t in tools]
                self._emit("log", f"Verfügbare MCP-Tools ({len(tools)}): {', '.join(names)}")
            except Exception as exc:
                self._emit("log", f"⚠ Tool-Listing nicht verfügbar: {exc}")

            # Batch-Verarbeitung (je Batch eine MCP-Session)
            batch_size = morningstar_client._BATCH_SIZE
            for i in range(0, total, batch_size):
                if self._stop_flag:
                    break
                batch = self._isins[i : i + batch_size]
                self._emit("log", f"Verarbeite ISINs {i+1}–{min(i+batch_size, total)} / {total} …")

                try:
                    results = morningstar_client.fetch_datapoints(
                        batch, self._field_keys, self._token
                    )
                except (ValueError, RuntimeError) as exc:
                    self._emit("error", str(exc))
                    self._failed += len(batch)
                    self._emit("progress", (self._done, self._failed, total))
                    continue

                for isin in batch:
                    data = results.get(isin.upper()) or results.get(isin)
                    if data:
                        morningstar_store.upsert_morningstar(isin, data)
                        self._done += 1
                        cat = data.get("ms_category", "")
                        self._emit("isin_done", (isin, data))
                        self._emit("log", f"  ✓ {isin}: {cat}")
                    else:
                        self._failed += 1
                        self._emit("log", f"  — {isin}: keine Daten von MCP")

                    self._emit("progress", (self._done, self._failed, total))

        except Exception as exc:
            self._emit("error", f"Unerwarteter Fehler: {exc}")
        finally:
            stopped = self._stop_flag
            self._emit("done", (self._done, self._failed, total))
            self._emit("log", (
                ("Abgebrochen. " if stopped else "Fertig. ") +
                f"Erfolgreich: {self._done}, Keine Daten: {self._failed}"
            ))


# ─── Fenster ─────────────────────────────────────────────────────────────────

class MorningstarWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("Morningstar Datenanreicherung")
        self.configure(bg=BG_MAIN)
        self.geometry("1000x700")
        self.minsize(800, 560)

        self._worker: MorningstarWorker | None = None
        self._event_queue: queue.Queue = queue.Queue()
        self._selected_keys: dict[str, tk.BooleanVar] = {}
        self._search_var = tk.StringVar()

        # Auswahl aus .env laden (kommagetrennte keys)
        saved = os.getenv("MS_SELECTED_DATAPOINTS", "")
        self._initial_selection = set(saved.split(",")) if saved else set(morningstar_client.DEFAULT_SELECTED)

        self._build_ui()
        self._refresh_table()
        self._poll_queue()

    # ─── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Toolbar
        toolbar = tk.Frame(self, bg=BG_PANEL)
        toolbar.pack(fill="x")
        inner = tk.Frame(toolbar, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        tk.Label(inner, text="🌟 Morningstar Datenanreicherung",
                 bg=BG_PANEL, fg=ACCENT_GOLD,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        self._btn_stop = tk.Button(
            inner, text="⏹  Stopp", command=self._stop_worker,
            bg="#3a2000", fg=ACCENT_YELLOW, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            state="disabled",
        )
        self._btn_stop.pack(side="right", padx=(4, 0))

        self._btn_update_all = tk.Button(
            inner, text="🔄  Alle aktualisieren", command=self._start_update_all,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
        )
        self._btn_update_all.pack(side="right", padx=(4, 0))

        self._btn_sel = tk.Button(
            inner, text="▶  Auswahl abrufen", command=self._start_selection,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
        )
        self._btn_sel.pack(side="right", padx=(4, 0))

        self._btn_missing = tk.Button(
            inner, text="📥  Alle fehlenden abrufen", command=self._start_missing,
            bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
        )
        self._btn_missing.pack(side="right", padx=(4, 0))

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # Fortschrittszeile
        prog_frame = tk.Frame(self, bg=BG_MAIN)
        prog_frame.pack(fill="x", padx=12, pady=(6, 2))
        self._prog_var = tk.DoubleVar(value=0)
        ttk.Progressbar(prog_frame, variable=self._prog_var, maximum=100, length=300
                        ).pack(side="left", padx=(0, 10))
        self._status_var = tk.StringVar(value="Bereit")
        tk.Label(prog_frame, textvariable=self._status_var,
                 bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9)).pack(side="left")
        tk.Button(prog_frame, text="↺ Aktualisieren", command=self._refresh_table,
                  bg=BTN_BG, fg=FG_TEXT, relief="flat",
                  font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2",
                  ).pack(side="right")

        # Notebook
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_PANEL, foreground=FG_MUTED,
                        padding=[12, 6], font=("Segoe UI", 9))
        style.map("TNotebook.Tab",
                  background=[("selected", BG_INPUT)],
                  foreground=[("selected", ACCENT_BLUE)])

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        tab_overview  = tk.Frame(nb, bg=BG_MAIN)
        tab_attributes = tk.Frame(nb, bg=BG_MAIN)
        tab_log       = tk.Frame(nb, bg=BG_MAIN)
        nb.add(tab_overview,   text="📊  Übersicht")
        nb.add(tab_attributes, text="🔧  Attribute")
        nb.add(tab_log,        text="📋  Log")

        self._build_tab_overview(tab_overview)
        self._build_tab_attributes(tab_attributes)
        self._build_tab_log(tab_log)

    def _build_tab_overview(self, parent):
        # Suchzeile
        flt = tk.Frame(parent, bg=BG_MAIN)
        flt.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(flt, text="Suche:", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        tk.Entry(flt, textvariable=self._search_var,
                 bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                 font=("Segoe UI", 9), relief="flat", bd=4, width=22,
                 ).pack(side="left")
        tk.Button(flt, text="✕", command=lambda: self._search_var.set(""),
                  bg=BTN_BG, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 8), padx=4, cursor="hand2",
                  ).pack(side="left", padx=(2, 0))
        self._match_label = tk.Label(flt, text="", bg=BG_MAIN, fg=FG_MUTED,
                                     font=("Segoe UI", 8))
        self._match_label.pack(side="right")

        self._search_var.trace_add("write", lambda *_: self._apply_filter())

        # Treeview
        tree_frame = tk.Frame(parent, bg=BG_MAIN)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        style = ttk.Style(self)
        style.configure("Treeview",
                        background=BG_PANEL, foreground=FG_TEXT,
                        fieldbackground=BG_PANEL, rowheight=24,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background=BG_INPUT, foreground=ACCENT_BLUE,
                        font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3a3a5e")])

        cols = [c[0] for c in _TREE_COLS]
        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                   selectmode="extended")
        self._tree.column("#0", width=0, stretch=False)
        for key, header, width in _TREE_COLS:
            self._tree.heading(key, text=header,
                               command=lambda c=key: self._sort_by(c))
            self._tree.column(key, width=width, minwidth=40, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

        self._tree.tag_configure("ok",      background="#1a2e1a", foreground=ACCENT_GREEN)
        self._tree.tag_configure("missing", background=BG_PANEL,  foreground=FG_MUTED)
        self._tree.tag_configure("error",   background="#2e1a1a", foreground=ACCENT_RED)

        self._all_table_rows: list[dict] = []
        self._sort_col = ""
        self._sort_rev = False

    def _build_tab_attributes(self, parent):
        header = tk.Frame(parent, bg=BG_MAIN)
        header.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(header, text="Welche Morningstar-Felder sollen abgerufen werden?",
                 bg=BG_MAIN, fg=ACCENT_BLUE,
                 font=("Segoe UI", 10, "bold")).pack(side="left")

        # Toggle-Buttons
        btn_frame = tk.Frame(parent, bg=BG_MAIN)
        btn_frame.pack(fill="x", padx=12, pady=(0, 8))
        tk.Button(btn_frame, text="Alle aktivieren",
                  command=lambda: self._toggle_all(True),
                  bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
                  font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2",
                  ).pack(side="left", padx=(0, 6))
        tk.Button(btn_frame, text="Alle deaktivieren",
                  command=lambda: self._toggle_all(False),
                  bg=BTN_BG, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2",
                  ).pack(side="left")
        tk.Button(btn_frame, text="💾  Auswahl speichern",
                  command=self._save_selection,
                  bg="#1a2e1a", fg=ACCENT_GREEN, relief="flat",
                  font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2",
                  ).pack(side="right")

        # Scrollbare Checkbutton-Liste
        canvas = tk.Canvas(parent, bg=BG_MAIN, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", padx=(0, 8))
        canvas.pack(fill="both", expand=True, padx=(12, 0))

        inner = tk.Frame(canvas, bg=BG_MAIN)
        cwin  = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cwin, width=e.width))
        inner.bind("<Configure>",  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        for key, (api_field, label) in morningstar_client.AVAILABLE_DATAPOINTS.items():
            var = tk.BooleanVar(value=(key in self._initial_selection))
            self._selected_keys[key] = var
            row = tk.Frame(inner, bg=BG_PANEL)
            row.pack(fill="x", pady=1)
            tk.Checkbutton(
                row, text=f"  {label}",
                variable=var,
                bg=BG_PANEL, fg=FG_TEXT, selectcolor=BG_INPUT,
                activebackground=BG_PANEL, activeforeground=ACCENT_BLUE,
                font=("Segoe UI", 9), anchor="w", cursor="hand2",
            ).pack(side="left", padx=8, pady=4)
            tk.Label(row, text=api_field,
                     bg=BG_PANEL, fg=FG_MUTED,
                     font=("Consolas", 8)).pack(side="right", padx=12)

    def _build_tab_log(self, parent):
        self._log = scrolledtext.ScrolledText(
            parent, bg=BG_INPUT, fg=FG_TEXT,
            font=("Consolas", 8), state="disabled",
            insertbackground=FG_TEXT, relief="flat",
        )
        self._log.pack(fill="both", expand=True, padx=8, pady=8)

    # ─── Tabelle ─────────────────────────────────────────────────────────────

    def _refresh_table(self):
        ms_data = {r["isin"]: r for r in morningstar_store.get_all_morningstar()}
        fund_isins = morningstar_store.get_all_isins()

        rows = []
        for isin in fund_isins:
            ms = ms_data.get(isin)
            if ms:
                rows.append({**ms, "status": "✓ Geladen", "_tag": "ok"})
            else:
                rows.append({
                    "isin": isin, "ms_category": "", "ms_rating": "",
                    "ms_investor_type": "", "ms_legal_type": "", "ms_fetch_date": "",
                    "status": "— Fehlt", "_tag": "missing",
                })
        self._all_table_rows = rows
        self._apply_filter()

    def _apply_filter(self):
        q = self._search_var.get().strip().lower()
        filtered = [
            r for r in self._all_table_rows
            if not q or q in (r.get("isin", "") + r.get("ms_category", "") +
                               r.get("status", "")).lower()
        ]
        self._populate_tree(filtered)
        total = len(self._all_table_rows)
        shown = len(filtered)
        self._match_label.config(
            text=f"{shown} / {total} ISINs" if shown < total else f"{total} ISINs",
            fg=ACCENT_YELLOW if shown < total else FG_MUTED,
        )

    def _populate_tree(self, rows: list[dict]):
        self._tree.delete(*self._tree.get_children())
        for r in rows:
            vals = tuple(r.get(c[0], "") for c in _TREE_COLS)
            self._tree.insert("", "end", iid=r["isin"], values=vals,
                              tags=(r.get("_tag", "missing"),))

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        self._all_table_rows.sort(
            key=lambda r: r.get(col, "") or "", reverse=self._sort_rev
        )
        self._apply_filter()

    # ─── Worker starten ──────────────────────────────────────────────────────

    def _credentials(self) -> str | None:
        """
        Gibt einen gültigen Access Token zurück oder None.
        Versucht automatisch, einen abgelaufenen Token via Refresh Token zu erneuern.
        """
        from datetime import datetime, timedelta

        token = os.getenv("MORNINGSTAR_ACCESS_TOKEN", "")
        if not token:
            messagebox.showerror(
                "Nicht angemeldet",
                "Bitte im Admin-Panel unter '🌟 Morningstar' anmelden\n"
                "(Schaltfläche '🌐 Im Browser anmelden').",
                parent=self,
            )
            return None

        # Ablauf prüfen
        expires_str = os.getenv("MORNINGSTAR_TOKEN_EXPIRES", "")
        if expires_str:
            try:
                expires = datetime.fromisoformat(expires_str)
                if datetime.now() >= expires:
                    # Refresh versuchen
                    refresh_tok = os.getenv("MORNINGSTAR_REFRESH_TOKEN", "")
                    token_url   = os.getenv("MORNINGSTAR_TOKEN_URL", "")
                    client_id   = os.getenv("MORNINGSTAR_CLIENT_ID", "")
                    if refresh_tok and token_url:
                        try:
                            new = morningstar_client.refresh_access_token(refresh_tok, token_url, client_id)
                            token = new["access_token"]
                            new_expires = (datetime.now() + timedelta(seconds=int(new.get("expires_in", 3600)))).isoformat()
                            from dotenv import set_key as _sk
                            _sk(".env", "MORNINGSTAR_ACCESS_TOKEN", token)
                            _sk(".env", "MORNINGSTAR_TOKEN_EXPIRES", new_expires)
                            os.environ["MORNINGSTAR_ACCESS_TOKEN"] = token
                            os.environ["MORNINGSTAR_TOKEN_EXPIRES"] = new_expires
                        except Exception:
                            messagebox.showwarning(
                                "Token abgelaufen",
                                "Morningstar-Token abgelaufen. Bitte im Admin-Panel neu anmelden.",
                                parent=self,
                            )
                            return None
                    else:
                        messagebox.showwarning(
                            "Token abgelaufen",
                            "Morningstar-Token abgelaufen. Bitte im Admin-Panel neu anmelden.",
                            parent=self,
                        )
                        return None
            except Exception:
                pass

        return token

    def _selected_field_keys(self) -> list[str]:
        return [k for k, v in self._selected_keys.items() if v.get()]

    def _start_missing(self):
        token = self._credentials()
        if not token:
            return
        isins = morningstar_store.get_isins_without_morningstar()
        if not isins:
            messagebox.showinfo("Alles erledigt",
                                "Alle ISINs haben bereits Morningstar-Daten.",
                                parent=self)
            return
        self._launch_worker(isins, token)

    def _start_selection(self):
        token = self._credentials()
        if not token:
            return
        isins = [self._tree.item(iid)["values"][0]
                 for iid in self._tree.selection()
                 if self._tree.item(iid)["values"]]
        if not isins:
            messagebox.showwarning("Keine Auswahl",
                                   "Bitte mindestens eine ISIN markieren.", parent=self)
            return
        self._launch_worker(isins, token)

    def _start_update_all(self):
        token = self._credentials()
        if not token:
            return
        isins = morningstar_store.get_all_isins()
        if not isins:
            messagebox.showinfo("Keine ISINs", "Keine ISINs in der Datenbank.", parent=self)
            return
        if not messagebox.askyesno(
            "Alle aktualisieren",
            f"{len(isins)} ISINs werden neu von Morningstar abgerufen.\nFortfahren?",
            parent=self,
        ):
            return
        self._launch_worker(isins, token)

    def _launch_worker(self, isins: list[str], token: str):
        if self._worker and self._worker.is_alive():
            return
        field_keys = self._selected_field_keys()
        if not field_keys:
            messagebox.showwarning("Keine Attribute",
                                   "Bitte mindestens ein Attribut im Tab 'Attribute' auswählen.",
                                   parent=self)
            return
        self._event_queue = queue.Queue()
        self._worker = MorningstarWorker(
            isins, field_keys, token,
            self._event_queue,
        )
        self._worker.start()
        self._set_running(True)
        self._status_var.set(f"0 / {len(isins)}")
        self._prog_var.set(0)

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self._btn_missing.config(state=state)
        self._btn_sel.config(state=state)
        self._btn_update_all.config(state=state)
        self._btn_stop.config(state="normal" if running else "disabled")
        if hasattr(self.master, "notify_process"):
            self.master.notify_process("Morningstar", running)

    # ─── Queue-Polling ────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                evt_type, payload = self._event_queue.get_nowait()
                if evt_type == "log":
                    self._log_line(payload)
                elif evt_type == "error":
                    self._log_line(f"✗ FEHLER: {payload}")
                    self._status_var.set(f"Fehler: {str(payload)[:60]}")
                elif evt_type == "isin_done":
                    isin, data = payload
                    self._update_tree_row(isin, data)
                elif evt_type == "progress":
                    done, failed, total = payload
                    if total > 0:
                        self._prog_var.set((done + failed) / total * 100)
                        self._status_var.set(
                            f"{done + failed} / {total}  ✓{done}  ✗{failed}"
                        )
                elif evt_type == "done":
                    done, failed, total = payload
                    self._set_running(False)
                    self._refresh_table()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _update_tree_row(self, isin: str, data: dict):
        if self._tree.exists(isin):
            vals = (
                isin,
                data.get("ms_category", ""),
                data.get("ms_rating", ""),
                data.get("ms_investor_type", ""),
                data.get("ms_legal_type", ""),
                data.get("ms_fetch_date", ""),
                "✓ Geladen",
            )
            try:
                self._tree.item(isin, values=vals, tags=("ok",))
            except tk.TclError:
                pass

    def _log_line(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")

    # ─── Attribut-Auswahl speichern ───────────────────────────────────────────

    def _toggle_all(self, value: bool):
        for var in self._selected_keys.values():
            var.set(value)

    def _save_selection(self):
        selected = ",".join(k for k, v in self._selected_keys.items() if v.get())
        env_path = Path(".env")
        if not env_path.exists():
            env_path.write_text("")
        set_key(".env", "MS_SELECTED_DATAPOINTS", selected)
        os.environ["MS_SELECTED_DATAPOINTS"] = selected
        messagebox.showinfo("Gespeichert",
                            f"{selected.count(',') + 1} Attribute gespeichert.",
                            parent=self)
