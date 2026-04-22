"""Admin-Panel: API-Key, Modell-Konfiguration und System-Einstellungen."""

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import set_key

BG_MAIN = "#1e1e2e"
BG_PANEL = "#2a2a3e"
BG_INPUT = "#313145"
FG_TEXT = "#cdd6f4"
FG_MUTED = "#7f849c"
ACCENT_BLUE = "#89b4fa"
ACCENT_GREEN = "#a6e3a1"
ACCENT_RED = "#f38ba8"
ACCENT_YELLOW = "#f9e2af"
ACCENT_LAVENDER = "#b4befe"
BTN_BG = "#45475a"
BTN_ACTIVE = "#585b70"

BATCH_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]
SINGLE_MODELS = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-7",
]


class AdminPanel(tk.Toplevel):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("Admin-Einstellungen")
        self.configure(bg=BG_MAIN)
        self.geometry("520x560")
        self.resizable(False, False)
        self.grab_set()

        self._parent = parent
        self._build_ui()
        self._load()

    def _section(self, parent, title: str) -> tk.Frame:
        tk.Label(
            parent, text=title, bg=BG_MAIN, fg=ACCENT_BLUE,
            font=("Segoe UI", 10, "bold"), anchor="w"
        ).pack(fill="x", padx=20, pady=(14, 2))
        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=20)
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill="x", padx=20, pady=(0, 4))
        return f

    def _field(self, parent, label: str, var: tk.StringVar,
               row: int, show: str = "") -> tk.Entry:
        parent.columnconfigure(1, weight=1)
        tk.Label(parent, text=label, bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=row, column=0, sticky="w", padx=12, pady=4)
        e = tk.Entry(parent, textvariable=var, bg=BG_INPUT, fg=FG_TEXT,
                     insertbackground=FG_TEXT, font=("Segoe UI", 9),
                     relief="flat", bd=4, show=show)
        e.grid(row=row, column=1, sticky="ew", padx=(4, 12), pady=4)
        return e

    def _combo(self, parent, label: str, var: tk.StringVar,
               values: list, row: int) -> ttk.Combobox:
        parent.columnconfigure(1, weight=1)
        tk.Label(parent, text=label, bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=row, column=0, sticky="w", padx=12, pady=4)
        cb = ttk.Combobox(parent, textvariable=var, values=values,
                          state="readonly", font=("Segoe UI", 9))
        cb.grid(row=row, column=1, sticky="ew", padx=(4, 12), pady=4)
        return cb

    def _build_ui(self):
        # ── API ───────────────────────────────────────────────────────
        api_frame = self._section(self, "API-Konfiguration")
        self.var_key = tk.StringVar()
        key_entry = self._field(api_frame, "API-Key:", self.var_key, 0, show="*")

        btn_row = tk.Frame(api_frame, bg=BG_PANEL)
        btn_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 6))

        tk.Button(
            btn_row, text="🔑  Anzeigen / Verbergen",
            command=lambda: key_entry.config(
                show="" if key_entry.cget("show") else "*"
            ),
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2"
        ).pack(side="left")

        self.validate_btn = tk.Button(
            btn_row, text="✔  Key testen",
            command=self._validate_key,
            bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2"
        )
        self.validate_btn.pack(side="left", padx=(6, 0))

        self.key_status = tk.Label(
            btn_row, text="", bg=BG_PANEL,
            font=("Segoe UI", 8)
        )
        self.key_status.pack(side="left", padx=(8, 0))

        # ── Modelle ───────────────────────────────────────────────────
        model_frame = self._section(self, "Modell-Konfiguration")
        self.var_batch_model = tk.StringVar()
        self.var_single_model = tk.StringVar()
        self._combo(model_frame, "Batch-Modell:", self.var_batch_model, BATCH_MODELS, 0)
        self._combo(model_frame, "Einzel-Modell:", self.var_single_model, SINGLE_MODELS, 1)

        # Kosten-Hinweis
        tk.Label(
            model_frame,
            text="Haiku = günstig/schnell  |  Sonnet = ausgewogen  |  Opus = präzise/teuer",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8)
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── Verarbeitung ──────────────────────────────────────────────
        proc_frame = self._section(self, "Verarbeitungs-Einstellungen")
        self.var_batch_size = tk.StringVar()
        self.var_delay = tk.StringVar()
        self.var_retries = tk.StringVar()
        self._field(proc_frame, "Batch-Grösse:", self.var_batch_size, 0)
        self._field(proc_frame, "Verzögerung (s):", self.var_delay, 1)
        self._field(proc_frame, "Max. Wiederholungen:", self.var_retries, 2)

        # ── System-Info ───────────────────────────────────────────────
        info_frame = self._section(self, "System-Info")
        db_path = Path(__file__).parent.parent / "data" / "output" / "results.db"

        try:
            import results_store
            stats = results_store.get_stats()
            db_info = (
                f"DB: {db_path.name}  |  "
                f"Einträge: {stats['total']}  |  "
                f"Retail: {stats['retail']}  |  "
                f"Institutional: {stats['institutional']}  |  "
                f"Unklar: {stats['unklar']}"
            )
        except Exception:
            db_info = f"DB: {db_path}"

        tk.Label(
            info_frame, text=db_info,
            bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w", wraplength=460, justify="left"
        ).pack(fill="x", padx=12, pady=6)

        # ── Buttons ───────────────────────────────────────────────────
        btn_bar = tk.Frame(self, bg=BG_MAIN)
        btn_bar.pack(fill="x", padx=20, pady=16)

        tk.Button(
            btn_bar, text="  Speichern  ",
            command=self._save,
            bg="#1e3a1e", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 10, "bold"), padx=14, pady=6, cursor="hand2"
        ).pack(side="right")

        tk.Button(
            btn_bar, text="  Abbrechen  ",
            command=self.destroy,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 10), padx=14, pady=6, cursor="hand2"
        ).pack(side="right", padx=(0, 8))

    def _load(self):
        self.var_key.set(os.getenv("ANTHROPIC_API_KEY", ""))
        self.var_batch_model.set(
            os.getenv("CLAUDE_BATCH_MODEL", "claude-haiku-4-5-20251001")
        )
        self.var_single_model.set(
            os.getenv("CLAUDE_SINGLE_MODEL", "claude-sonnet-4-6")
        )
        self.var_batch_size.set(os.getenv("BATCH_SIZE", "200"))
        self.var_delay.set(os.getenv("REQUEST_DELAY", "1.5"))
        self.var_retries.set(os.getenv("MAX_RETRIES", "3"))

    def _validate_key(self):
        key = self.var_key.get().strip()
        if not key:
            self.key_status.config(text="Kein Key eingegeben", fg=ACCENT_YELLOW)
            return
        self.key_status.config(text="Wird geprüft...", fg=FG_MUTED)
        self.validate_btn.config(state="disabled")

        def check():
            try:
                from claude_classifier import validate_api_key
                ok = validate_api_key(key)
            except Exception:
                ok = False
            self.after(0, lambda: self._on_validate_done(ok))

        threading.Thread(target=check, daemon=True).start()

    def _on_validate_done(self, ok: bool):
        self.validate_btn.config(state="normal")
        if ok:
            self.key_status.config(text="✔ Gültig", fg=ACCENT_GREEN)
        else:
            self.key_status.config(text="✘ Ungültig", fg=ACCENT_RED)

    def _save(self):
        env_path = Path(".env")
        if not env_path.exists():
            env_path.write_text("")

        key = self.var_key.get().strip()
        if key:
            set_key(".env", "ANTHROPIC_API_KEY", key)
            os.environ["ANTHROPIC_API_KEY"] = key

        set_key(".env", "CLAUDE_BATCH_MODEL", self.var_batch_model.get())
        set_key(".env", "CLAUDE_SINGLE_MODEL", self.var_single_model.get())
        set_key(".env", "BATCH_SIZE", self.var_batch_size.get().strip())
        set_key(".env", "REQUEST_DELAY", self.var_delay.get().strip())
        set_key(".env", "MAX_RETRIES", self.var_retries.get().strip())

        # Hauptfenster-Felder synchronisieren
        try:
            if key and hasattr(self._parent, "var_api_key"):
                self._parent.var_api_key.set(key)
            if hasattr(self._parent, "var_batch_size"):
                self._parent.var_batch_size.set(self.var_batch_size.get().strip())
        except Exception:
            pass

        messagebox.showinfo("Gespeichert", "Einstellungen wurden gespeichert.", parent=self)
        self.destroy()
