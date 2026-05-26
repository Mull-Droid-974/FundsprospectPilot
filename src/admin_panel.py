"""Admin-Panel: API-Key, Modell-Konfiguration und System-Einstellungen."""

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
        self.geometry("540x460")
        self.resizable(False, False)
        self.grab_set()

        self._parent = parent
        self._build_ui()
        self._load()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _section(self, parent, title: str) -> tk.Frame:
        tk.Label(
            parent, text=title, bg=BG_MAIN, fg=ACCENT_BLUE,
            font=("Segoe UI", 10, "bold"), anchor="w"
        ).pack(fill="x", padx=16, pady=(14, 2))
        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=16)
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill="x", padx=16, pady=(0, 4))
        return f

    def _field(self, parent, label: str, var: tk.StringVar,
               row: int, show: str = "") -> tk.Entry:
        parent.columnconfigure(1, weight=1)
        tk.Label(parent, text=label, bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=row, column=0, sticky="w", padx=12, pady=5)
        e = tk.Entry(parent, textvariable=var, bg=BG_INPUT, fg=FG_TEXT,
                     insertbackground=FG_TEXT, font=("Segoe UI", 9),
                     relief="flat", bd=4, show=show)
        e.grid(row=row, column=1, sticky="ew", padx=(4, 12), pady=5)
        return e

    def _combo(self, parent, label: str, var: tk.StringVar,
               values: list, row: int) -> ttk.Combobox:
        parent.columnconfigure(1, weight=1)
        tk.Label(parent, text=label, bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=row, column=0, sticky="w", padx=12, pady=5)
        cb = ttk.Combobox(parent, textvariable=var, values=values,
                          state="readonly", font=("Segoe UI", 9))
        cb.grid(row=row, column=1, sticky="ew", padx=(4, 12), pady=5)
        return cb

    def _key_btn_row(self, parent, key_entry, show_cmd, validate_cmd, grid_row: int = 1):
        """Gemeinsame Zeile: Anzeigen/Verbergen + Key testen + Status."""
        row_frame = tk.Frame(parent, bg=BG_PANEL)
        row_frame.grid(row=grid_row, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))

        tk.Button(
            row_frame, text="🔑  Anzeigen / Verbergen",
            command=show_cmd,
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2"
        ).pack(side="left")

        btn = tk.Button(
            row_frame, text="✔  Key testen",
            command=validate_cmd,
            bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2"
        )
        btn.pack(side="left", padx=(6, 0))

        status = tk.Label(row_frame, text="", bg=BG_PANEL, font=("Segoe UI", 8))
        status.pack(side="left", padx=(8, 0))
        return btn, status

    # ─── Tab-Bau ──────────────────────────────────────────────────────────────

    def _build_tab_dateien(self, parent):
        frame = self._section(parent, "Datei-Konfiguration")
        frame.columnconfigure(1, weight=1)
        self.var_excel = tk.StringVar()
        self.var_pdf_folder = tk.StringVar()

        self._field(frame, "Excel-Datei:", self.var_excel, 0)
        tk.Button(
            frame, text="...", command=self._browse_excel,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 8), padx=6, cursor="hand2"
        ).grid(row=0, column=2, padx=(4, 8), pady=5)

        self._field(frame, "PDF-Ordner:", self.var_pdf_folder, 1)
        tk.Button(
            frame, text="...", command=self._browse_pdf_folder,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 8), padx=6, cursor="hand2"
        ).grid(row=1, column=2, padx=(4, 8), pady=5)

    def _build_tab_anthropic(self, parent):
        api_frame = self._section(parent, "API-Key (Anthropic)")
        api_frame.columnconfigure(1, weight=1)
        self.var_key = tk.StringVar()
        key_entry = self._field(api_frame, "API-Key:", self.var_key, 0, show="*")
        self.validate_btn, self.key_status = self._key_btn_row(
            api_frame, key_entry,
            show_cmd=lambda: key_entry.config(show="" if key_entry.cget("show") else "*"),
            validate_cmd=self._validate_key,
            grid_row=1,
        )

        model_frame = self._section(parent, "Modelle")
        self.var_batch_model = tk.StringVar()
        self.var_single_model = tk.StringVar()
        self._combo(model_frame, "Batch-Modell:", self.var_batch_model, BATCH_MODELS, 0)
        self._combo(model_frame, "Einzel-Modell:", self.var_single_model, SINGLE_MODELS, 1)
        tk.Label(
            model_frame,
            text="Haiku = günstig/schnell  |  Sonnet = ausgewogen  |  Opus = präzise/teuer",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8)
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

    def _build_tab_gemini(self, parent):
        from llm_provider import MODELS as LLM_MODELS

        api_frame = self._section(parent, "API-Key (Google Gemini)")
        api_frame.columnconfigure(1, weight=1)
        self.var_gemini_key = tk.StringVar()
        gemini_key_entry = self._field(api_frame, "API-Key:", self.var_gemini_key, 0, show="*")
        self.validate_gemini_btn, self.gemini_key_status = self._key_btn_row(
            api_frame, gemini_key_entry,
            show_cmd=lambda: gemini_key_entry.config(
                show="" if gemini_key_entry.cget("show") else "*"
            ),
            validate_cmd=self._validate_gemini_key,
            grid_row=1,
        )

        model_frame = self._section(parent, "Modelle")
        self.var_gemini_batch = tk.StringVar()
        self.var_gemini_single = tk.StringVar()
        self._combo(model_frame, "Batch-Modell:", self.var_gemini_batch, LLM_MODELS["gemini"], 0)
        self._combo(model_frame, "Einzel-Modell:", self.var_gemini_single, LLM_MODELS["gemini"], 1)
        tk.Label(
            model_frame,
            text="Flash = günstig/schnell  |  Pro = ausgewogen/präzise",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8)
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

    def _build_tab_openrouter(self, parent):
        from llm_provider import OPENROUTER_MODELS

        api_frame = self._section(parent, "API-Key (OpenRouter)")
        api_frame.columnconfigure(1, weight=1)
        self.var_openrouter_key = tk.StringVar()
        or_key_entry = self._field(api_frame, "API-Key:", self.var_openrouter_key, 0, show="*")
        self.validate_or_btn, self.or_key_status = self._key_btn_row(
            api_frame, or_key_entry,
            show_cmd=lambda: or_key_entry.config(
                show="" if or_key_entry.cget("show") else "*"
            ),
            validate_cmd=self._validate_openrouter_key,
            grid_row=1,
        )

        model_frame = self._section(parent, "Modelle")
        self.var_or_batch = tk.StringVar()
        self.var_or_single = tk.StringVar()
        # state="normal" ermöglicht freie Eingabe (z.B. neue OpenRouter-Modelle)
        tk.Label(model_frame, text="Batch-Modell:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=0, column=0, sticky="w", padx=12, pady=5)
        cb_batch = ttk.Combobox(model_frame, textvariable=self.var_or_batch,
                                values=OPENROUTER_MODELS, state="normal",
                                font=("Segoe UI", 9))
        cb_batch.grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=5)

        tk.Label(model_frame, text="Einzel-Modell:", bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=1, column=0, sticky="w", padx=12, pady=5)
        cb_single = ttk.Combobox(model_frame, textvariable=self.var_or_single,
                                 values=OPENROUTER_MODELS, state="normal",
                                 font=("Segoe UI", 9))
        cb_single.grid(row=1, column=1, sticky="ew", padx=(4, 12), pady=5)

        tk.Label(
            model_frame,
            text="Modell-ID frei eingeben oder aus Liste wählen  |  Alle OpenRouter-Modelle nutzbar",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8)
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

    def _build_tab_morningstar(self, parent):
        api_frame = self._section(parent, "OAuth2 Credentials (Morningstar Direct)")
        api_frame.columnconfigure(1, weight=1)
        self.var_ms_client_id = tk.StringVar()
        self.var_ms_client_secret = tk.StringVar()
        self._field(api_frame, "Client ID:", self.var_ms_client_id, 0)
        ms_secret_entry = self._field(api_frame, "Client Secret:", self.var_ms_client_secret, 1, show="*")

        btn_row = tk.Frame(api_frame, bg=BG_PANEL)
        btn_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))
        tk.Button(
            btn_row, text="🔑  Anzeigen / Verbergen",
            command=lambda: ms_secret_entry.config(
                show="" if ms_secret_entry.cget("show") else "*"
            ),
            bg=BTN_BG, fg=FG_MUTED, relief="flat",
            font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2"
        ).pack(side="left")
        self.validate_ms_btn = tk.Button(
            btn_row, text="✔  Verbindung testen",
            command=self._validate_ms_connection,
            bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2"
        )
        self.validate_ms_btn.pack(side="left", padx=(6, 0))
        self.ms_status = tk.Label(btn_row, text="", bg=BG_PANEL, font=("Segoe UI", 8))
        self.ms_status.pack(side="left", padx=(8, 0))

        url_frame = self._section(parent, "Endpoint-URLs")
        url_frame.columnconfigure(1, weight=1)
        self.var_ms_token_url = tk.StringVar()
        self.var_ms_data_url = tk.StringVar()
        self._field(url_frame, "Token-URL:", self.var_ms_token_url, 0)
        self._field(url_frame, "Daten-URL:", self.var_ms_data_url, 1)
        tk.Label(
            url_frame,
            text="Endpoint-URLs aus der Morningstar Direct Web Services Dokumentation entnehmen.",
            bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 8),
            wraplength=440, justify="left"
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

    def _build_tab_system(self, parent):
        proc_frame = self._section(parent, "Verarbeitungs-Einstellungen")
        self.var_batch_size = tk.StringVar()
        self.var_delay = tk.StringVar()
        self.var_retries = tk.StringVar()
        self._field(proc_frame, "Batch-Grösse:", self.var_batch_size, 0)
        self._field(proc_frame, "Verzögerung (s):", self.var_delay, 1)
        self._field(proc_frame, "Max. Wiederholungen:", self.var_retries, 2)

        db_frame = self._section(parent, "Datenbank")
        db_path = Path(__file__).parent.parent / "data" / "output" / "results.db"
        try:
            import results_store
            stats = results_store.get_stats()
            db_info = (
                f"DB: {db_path.name}  |  Einträge: {stats['total']}  |  "
                f"Retail: {stats['retail']}  |  "
                f"Institutional: {stats['institutional']}  |  "
                f"Unklar: {stats['unklar']}"
            )
        except Exception:
            db_info = f"DB: {db_path}"

        tk.Label(
            db_frame, text=db_info,
            bg=BG_PANEL, fg=FG_MUTED,
            font=("Segoe UI", 8), anchor="w", wraplength=460, justify="left"
        ).pack(fill="x", padx=12, pady=(8, 4))

        tk.Button(
            db_frame, text="🗑  Alle Daten löschen",
            command=self._delete_all_data,
            bg="#2e0000", fg=ACCENT_RED, relief="flat",
            font=("Segoe UI", 9), padx=8, pady=4, cursor="hand2"
        ).pack(anchor="w", padx=12, pady=(0, 10))

    # ─── Haupt-UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG_MAIN, borderwidth=0, tabmargins=[0, 0, 0, 0])
        style.configure("TNotebook.Tab",
                        background=BG_PANEL, foreground=FG_MUTED,
                        padding=[14, 7], font=("Segoe UI", 9))
        style.map("TNotebook.Tab",
                  background=[("selected", BG_INPUT)],
                  foreground=[("selected", ACCENT_BLUE)])

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        tabs = [
            ("📁  Dateien",     self._build_tab_dateien),
            ("🔑  Anthropic",   self._build_tab_anthropic),
            ("🤖  Gemini",      self._build_tab_gemini),
            ("🌐  OpenRouter",  self._build_tab_openrouter),
            ("🌟  Morningstar", self._build_tab_morningstar),
            ("⚙  System",      self._build_tab_system),
        ]
        for label, builder in tabs:
            tab = tk.Frame(nb, bg=BG_MAIN)
            nb.add(tab, text=label)
            builder(tab)

        btn_bar = tk.Frame(self, bg=BG_MAIN)
        btn_bar.pack(fill="x", padx=16, pady=(4, 12))

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

    # ─── Aktionen ─────────────────────────────────────────────────────────────

    def _browse_excel(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="Excel-Datei wählen",
            filetypes=[("Excel", "*.xlsx *.xls"), ("Alle Dateien", "*.*")],
        )
        if path:
            self.var_excel.set(path)

    def _browse_pdf_folder(self):
        path = filedialog.askdirectory(parent=self, title="PDF-Ordner wählen")
        if path:
            self.var_pdf_folder.set(path)

    def _load(self):
        self.var_excel.set(os.getenv("EXCEL_PATH", "data/input/fonds_universe.xlsx"))
        self.var_pdf_folder.set(os.getenv("PDF_FOLDER", "data/prospectus"))
        self.var_key.set(os.getenv("ANTHROPIC_API_KEY", ""))
        self.var_batch_model.set(os.getenv("CLAUDE_BATCH_MODEL", "claude-haiku-4-5-20251001"))
        self.var_single_model.set(os.getenv("CLAUDE_SINGLE_MODEL", "claude-sonnet-4-6"))
        self.var_batch_size.set(os.getenv("BATCH_SIZE", "200"))
        self.var_delay.set(os.getenv("REQUEST_DELAY", "1.5"))
        self.var_retries.set(os.getenv("MAX_RETRIES", "3"))
        self.var_gemini_key.set(os.getenv("GOOGLE_API_KEY", ""))
        from llm_provider import DEFAULT_BATCH_MODELS, DEFAULT_SINGLE_MODELS
        self.var_gemini_batch.set(os.getenv("GEMINI_BATCH_MODEL", DEFAULT_BATCH_MODELS["gemini"]))
        self.var_gemini_single.set(os.getenv("GEMINI_SINGLE_MODEL", DEFAULT_SINGLE_MODELS["gemini"]))
        self.var_openrouter_key.set(os.getenv("OPENROUTER_API_KEY", ""))
        self.var_or_batch.set(os.getenv("OPENROUTER_BATCH_MODEL", DEFAULT_BATCH_MODELS["openrouter"]))
        self.var_or_single.set(os.getenv("OPENROUTER_SINGLE_MODEL", DEFAULT_SINGLE_MODELS["openrouter"]))
        self.var_ms_client_id.set(os.getenv("MORNINGSTAR_CLIENT_ID", ""))
        self.var_ms_client_secret.set(os.getenv("MORNINGSTAR_CLIENT_SECRET", ""))
        self.var_ms_token_url.set(os.getenv("MORNINGSTAR_TOKEN_URL", ""))
        self.var_ms_data_url.set(os.getenv("MORNINGSTAR_DATA_URL", ""))

    def _validate_key(self):
        key = self.var_key.get().strip()
        if not key:
            self.key_status.config(text="Kein Key eingegeben", fg=ACCENT_YELLOW)
            return
        self.key_status.config(text="Wird geprüft...", fg=FG_MUTED)
        self.validate_btn.config(state="disabled")

        def check():
            try:
                from utils import validate_api_key
                ok = validate_api_key(key)
            except Exception:
                ok = False
            self.after(0, lambda: self._on_validate_done(ok))

        threading.Thread(target=check, daemon=True).start()

    def _on_validate_done(self, ok: bool):
        self.validate_btn.config(state="normal")
        self.key_status.config(
            text="✔ Gültig" if ok else "✘ Ungültig",
            fg=ACCENT_GREEN if ok else ACCENT_RED,
        )

    def _validate_gemini_key(self):
        key = self.var_gemini_key.get().strip()
        if not key:
            self.gemini_key_status.config(text="Kein Key eingegeben", fg=ACCENT_YELLOW)
            return
        self.gemini_key_status.config(text="Wird geprüft...", fg=FG_MUTED)
        self.validate_gemini_btn.config(state="disabled")

        def check():
            try:
                from llm_provider import validate_key
                ok, _ = validate_key(key, "gemini")
            except Exception:
                ok = False
            self.after(0, lambda: self._on_gemini_validate_done(ok))

        threading.Thread(target=check, daemon=True).start()

    def _on_gemini_validate_done(self, ok: bool):
        self.validate_gemini_btn.config(state="normal")
        self.gemini_key_status.config(
            text="✔ Gültig" if ok else "✘ Ungültig",
            fg=ACCENT_GREEN if ok else ACCENT_RED,
        )

    def _validate_openrouter_key(self):
        key = self.var_openrouter_key.get().strip()
        if not key:
            self.or_key_status.config(text="Kein Key eingegeben", fg=ACCENT_YELLOW)
            return
        self.or_key_status.config(text="Wird geprüft...", fg=FG_MUTED)
        self.validate_or_btn.config(state="disabled")

        def check():
            try:
                from llm_provider import validate_key
                ok, _ = validate_key(key, "openrouter")
            except Exception:
                ok = False
            self.after(0, lambda: self._on_or_validate_done(ok))

        threading.Thread(target=check, daemon=True).start()

    def _on_or_validate_done(self, ok: bool):
        self.validate_or_btn.config(state="normal")
        self.or_key_status.config(
            text="✔ Gültig" if ok else "✘ Ungültig",
            fg=ACCENT_GREEN if ok else ACCENT_RED,
        )

    def _validate_ms_connection(self):
        client_id = self.var_ms_client_id.get().strip()
        client_secret = self.var_ms_client_secret.get().strip()
        token_url = self.var_ms_token_url.get().strip()
        if not all([client_id, client_secret, token_url]):
            self.ms_status.config(text="Client ID, Secret und Token-URL erforderlich", fg=ACCENT_YELLOW)
            return
        self.ms_status.config(text="Verbindung wird geprüft...", fg=FG_MUTED)
        self.validate_ms_btn.config(state="disabled")

        def check():
            try:
                from morningstar_client import get_token
                get_token(client_id, client_secret, token_url)
                ok, msg = True, "✔ Token erhalten"
            except ValueError as exc:
                ok, msg = False, f"✘ {exc}"
            except Exception as exc:
                ok, msg = False, f"✘ {str(exc)[:60]}"
            self.after(0, lambda: self._on_ms_validate_done(ok, msg))

        threading.Thread(target=check, daemon=True).start()

    def _on_ms_validate_done(self, ok: bool, msg: str):
        self.validate_ms_btn.config(state="normal")
        self.ms_status.config(text=msg, fg=ACCENT_GREEN if ok else ACCENT_RED)

    def _delete_all_data(self):
        try:
            import results_store
            n = results_store.get_stats().get("total", 0)
        except Exception:
            n = 0
        if not messagebox.askokcancel(
            "Warnung",
            f"Alle {n} Einträge unwiderruflich löschen?",
            parent=self,
        ):
            return
        try:
            import results_store
            deleted = results_store.delete_all_results()
            messagebox.showinfo("Erledigt", f"{deleted} Einträge gelöscht.", parent=self)
        except Exception as exc:
            messagebox.showerror("Fehler", str(exc), parent=self)

    def _save(self):
        env_path = Path(".env")
        if not env_path.exists():
            env_path.write_text("")

        excel = self.var_excel.get().strip()
        if excel:
            set_key(".env", "EXCEL_PATH", excel)
            os.environ["EXCEL_PATH"] = excel

        pdf_folder = self.var_pdf_folder.get().strip()
        if pdf_folder:
            set_key(".env", "PDF_FOLDER", pdf_folder)
            os.environ["PDF_FOLDER"] = pdf_folder

        key = self.var_key.get().strip()
        if key:
            set_key(".env", "ANTHROPIC_API_KEY", key)
            os.environ["ANTHROPIC_API_KEY"] = key

        set_key(".env", "CLAUDE_BATCH_MODEL", self.var_batch_model.get())
        set_key(".env", "CLAUDE_SINGLE_MODEL", self.var_single_model.get())
        set_key(".env", "BATCH_SIZE", self.var_batch_size.get().strip())
        set_key(".env", "REQUEST_DELAY", self.var_delay.get().strip())
        set_key(".env", "MAX_RETRIES", self.var_retries.get().strip())

        gemini_key = self.var_gemini_key.get().strip()
        if gemini_key:
            set_key(".env", "GOOGLE_API_KEY", gemini_key)
            os.environ["GOOGLE_API_KEY"] = gemini_key

        set_key(".env", "GEMINI_BATCH_MODEL", self.var_gemini_batch.get())
        set_key(".env", "GEMINI_SINGLE_MODEL", self.var_gemini_single.get())

        or_key = self.var_openrouter_key.get().strip()
        if or_key:
            set_key(".env", "OPENROUTER_API_KEY", or_key)
            os.environ["OPENROUTER_API_KEY"] = or_key

        set_key(".env", "OPENROUTER_BATCH_MODEL", self.var_or_batch.get())
        set_key(".env", "OPENROUTER_SINGLE_MODEL", self.var_or_single.get())

        ms_client_id = self.var_ms_client_id.get().strip()
        if ms_client_id:
            set_key(".env", "MORNINGSTAR_CLIENT_ID", ms_client_id)
            os.environ["MORNINGSTAR_CLIENT_ID"] = ms_client_id

        ms_client_secret = self.var_ms_client_secret.get().strip()
        if ms_client_secret:
            set_key(".env", "MORNINGSTAR_CLIENT_SECRET", ms_client_secret)
            os.environ["MORNINGSTAR_CLIENT_SECRET"] = ms_client_secret

        ms_token_url = self.var_ms_token_url.get().strip()
        if ms_token_url:
            set_key(".env", "MORNINGSTAR_TOKEN_URL", ms_token_url)
            os.environ["MORNINGSTAR_TOKEN_URL"] = ms_token_url

        ms_data_url = self.var_ms_data_url.get().strip()
        if ms_data_url:
            set_key(".env", "MORNINGSTAR_DATA_URL", ms_data_url)
            os.environ["MORNINGSTAR_DATA_URL"] = ms_data_url

        messagebox.showinfo("Gespeichert", "Einstellungen wurden gespeichert.", parent=self)
        self.destroy()
