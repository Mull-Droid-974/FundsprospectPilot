"""Datenverwaltungs-Fenster: ISIN-Import, Ergebnis-Übersicht und Excel-Export."""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import results_store

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


class DataManagementWindow(tk.Toplevel):
    """
    Datenverwaltungs-Fenster mit zwei Tabs:
    - Import: ISIN-Grundmenge aus Excel laden
    - Ergebnisse: DB-Übersicht + Export
    """

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("Datenverwaltung")
        self.configure(bg=BG_MAIN)
        self.geometry("720x520")
        self.minsize(600, 400)

        self._build_ui()
        self.refresh_results()

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=12, pady=12)

        # Tab 1: Import
        self._import_tab = tk.Frame(notebook, bg=BG_MAIN)
        notebook.add(self._import_tab, text="  📥  ISIN-Import  ")
        self._build_import_tab(self._import_tab)

        # Tab 2: Ergebnisse
        self._results_tab = tk.Frame(notebook, bg=BG_MAIN)
        notebook.add(self._results_tab, text="  📊  Ergebnisse  ")
        self._build_results_tab(self._results_tab)

    # ── Import-Tab ────────────────────────────────────────────────────────────

    def _build_import_tab(self, parent):
        tk.Label(
            parent,
            text=(
                "Importiert ISINs aus einer Excel-Datei als Grundmenge.\n"
                "Erwartet Spalten: A=ISIN, B=GroupInvestment (Fondsname), "
                "C=Morningstar_Segmentierung.\n"
                "Bereits vorhandene ISINs werden nicht überschrieben."
            ),
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9),
            justify="left", anchor="w", wraplength=660
        ).pack(anchor="w", padx=16, pady=(12, 8))

        # Dateiauswahl
        file_row = tk.Frame(parent, bg=BG_MAIN)
        file_row.pack(fill="x", padx=16, pady=4)

        tk.Label(file_row, text="Excel-Datei:", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        self.var_import_path = tk.StringVar()
        tk.Entry(file_row, textvariable=self.var_import_path,
                 bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                 font=("Segoe UI", 9), relief="flat", bd=4, width=50
                 ).pack(side="left", padx=(8, 4), fill="x", expand=True)
        tk.Button(file_row, text="...", command=self._browse_import,
                  bg=BTN_BG, fg=FG_TEXT, relief="flat",
                  font=("Segoe UI", 8), padx=6, cursor="hand2"
                  ).pack(side="left")

        # Vorschau-Info
        self._import_info = tk.Label(
            parent, text="", bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9), anchor="w"
        )
        self._import_info.pack(anchor="w", padx=16, pady=4)

        # Import-Button
        tk.Button(
            parent, text="  ▶  Jetzt importieren  ",
            command=self._run_import,
            bg="#1e3a1e", fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 10, "bold"), padx=14, pady=6, cursor="hand2"
        ).pack(anchor="w", padx=16, pady=(4, 0))

        # Fortschritt
        self._import_log = tk.Text(
            parent, bg=BG_PANEL, fg=FG_TEXT,
            font=("Consolas", 8), relief="flat",
            state="disabled", wrap="word", height=10
        )
        self._import_log.pack(fill="both", expand=True, padx=16, pady=12)

    def _browse_import(self):
        path = filedialog.askopenfilename(
            title="Excel-Datei wählen",
            filetypes=[("Excel", "*.xlsx *.xls"), ("Alle Dateien", "*.*")]
        )
        if path:
            self.var_import_path.set(path)
            self._preview_import(path)

    def _preview_import(self, path: str):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            rows = sum(1 for _ in ws.iter_rows(min_row=2)) if ws else 0
            wb.close()
            self._import_info.config(
                text=f"Datei: {Path(path).name}  |  ~{rows} Zeilen gefunden",
                fg=ACCENT_BLUE
            )
        except Exception as e:
            self._import_info.config(text=f"Fehler beim Lesen: {e}", fg=ACCENT_RED)

    def _run_import(self):
        path = self.var_import_path.get().strip()
        if not path:
            messagebox.showerror("Fehler", "Bitte Excel-Datei wählen.", parent=self)
            return

        self._log_import("Starte Import...", clear=True)

        def worker():
            try:
                import openpyxl
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                ws = wb.active
                rows_data = []
                for row in ws.iter_rows(min_row=2, values_only=True):
                    isin = str(row[0]).strip() if row[0] else ""
                    fondsname = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                    pruef_seg = str(row[2]).strip() if len(row) > 2 and row[2] else ""
                    if isin and isin != "None":
                        rows_data.append({
                            "isin": isin,
                            "fund_id": "",
                            "fondsname": fondsname,
                            "pruef_segmentierung": pruef_seg,
                        })
                wb.close()

                self.after(0, lambda: self._log_import(f"{len(rows_data)} ISINs gelesen, importiere..."))
                imported, skipped = results_store.import_base_set(rows_data)
                self.after(0, lambda: self._log_import(
                    f"✅ Fertig: {imported} neu importiert, {skipped} übersprungen."
                ))
                self.after(0, self.refresh_results)

            except Exception as e:
                self.after(0, lambda: self._log_import(f"❌ Fehler: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _log_import(self, msg: str, clear: bool = False):
        self._import_log.config(state="normal")
        if clear:
            self._import_log.delete("1.0", "end")
        self._import_log.insert("end", msg + "\n")
        self._import_log.see("end")
        self._import_log.config(state="disabled")

    # ── Ergebnisse-Tab ────────────────────────────────────────────────────────

    def _build_results_tab(self, parent):
        # Statistik-Zeile
        self._stats_label = tk.Label(
            parent, text="", bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9), anchor="w"
        )
        self._stats_label.pack(anchor="w", padx=16, pady=(10, 4))

        # Treeview
        cols = ("isin", "fondsname", "segmentierung", "konfidenz", "analysiert_am")
        headers = ("ISIN", "Fondsname", "Segmentierung", "Konfidenz", "Analysiert am")
        widths = (130, 260, 110, 80, 130)

        tree_frame = tk.Frame(parent, bg=BG_MAIN)
        tree_frame.pack(fill="both", expand=True, padx=16, pady=(0, 4))

        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=14)
        for col, hdr, w in zip(cols, headers, widths):
            self._tree.heading(col, text=hdr)
            self._tree.column(col, width=w, minwidth=60)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        # Button-Leiste
        btn_row = tk.Frame(parent, bg=BG_MAIN)
        btn_row.pack(fill="x", padx=16, pady=(0, 12))

        tk.Button(
            btn_row, text="↺  Aktualisieren", command=self.refresh_results,
            bg=BTN_BG, fg=FG_TEXT, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2"
        ).pack(side="left")

        tk.Button(
            btn_row, text="📤  Excel-Export", command=self._export_excel,
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2"
        ).pack(side="left", padx=(6, 0))

        tk.Button(
            btn_row, text="🗑  Eintrag löschen", command=self._delete_selected,
            bg=BTN_BG, fg=ACCENT_RED, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2"
        ).pack(side="right")

    def refresh_results(self):
        """Lädt DB-Daten neu und aktualisiert die Treeview."""
        try:
            rows = results_store.get_all_results()
            stats = results_store.get_stats()

            self._stats_label.config(
                text=(
                    f"Gesamt: {stats['total']}  |  "
                    f"Retail: {stats['retail']}  |  "
                    f"Institutional: {stats['institutional']}  |  "
                    f"Unklar: {stats['unklar']}"
                )
            )

            self._tree.delete(*self._tree.get_children())
            for r in rows:
                self._tree.insert("", "end", values=(
                    r.get("isin", ""),
                    r.get("fondsname", ""),
                    r.get("segmentierung", ""),
                    r.get("konfidenz", ""),
                    r.get("analysiert_am", ""),
                ))
        except Exception:
            pass

    def _export_excel(self):
        path = filedialog.asksaveasfilename(
            title="Excel speichern",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")]
        )
        if not path:
            return
        try:
            results_store.export_to_excel(path)
            messagebox.showinfo("Export", f"Exportiert nach:\n{path}", parent=self)
        except Exception as e:
            messagebox.showerror("Fehler", str(e), parent=self)

    def _delete_selected(self):
        selected = self._tree.selection()
        if not selected:
            return
        isin = self._tree.item(selected[0])["values"][0]
        if not messagebox.askyesno("Löschen", f"ISIN {isin} wirklich löschen?", parent=self):
            return
        results_store.delete_result(str(isin))
        self.refresh_results()
