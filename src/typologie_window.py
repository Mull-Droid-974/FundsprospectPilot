"""
Taxonomie-Verwaltungsfenster.

Drei Tabs (Fondstyp / Anlegertyp / Kundentyp) mit editierbaren Werte-Listen.
Wird aus dem ProspektAnalysisWindow als Toplevel geöffnet.
"""

import tkinter as tk
from tkinter import messagebox, ttk

import typologie_store

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
BTN_BG          = "#45475a"
BTN_ACTIVE      = "#585b70"

_FELDER = [
    ("fondstyp",   "Fondstyp"),
    ("anlegertyp", "Anlegertyp"),
    ("kundentyp",  "Kundentyp"),
]

_SEGMENT_OPTS = ["retail", "institutional", ""]

_COLS = [
    ("wert",     "Wert / Ausprägung",  280),
    ("segment",  "Segment",             90),
    ("synonyme", "Synonyme / Englisch", 350),
]


class TypologieWindow(tk.Toplevel):

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("Werte-Verwaltung — Fondstyp / Anlegertyp / Kundentyp")
        self.configure(bg=BG_MAIN)
        self.geometry("800x560")
        self.minsize(650, 400)
        self.transient(parent)

        self._trees: dict[str, ttk.Treeview] = {}
        self._build_ui()
        self._refresh_all()

    # ─── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        tk.Label(
            self,
            text="Kanonisches Werte-Universum für die LLM-Analyse",
            bg=BG_MAIN, fg=ACCENT_LAVENDER,
            font=("Segoe UI", 10, "bold"),
        ).pack(fill="x", padx=12, pady=(10, 4))

        tk.Label(
            self,
            text="Änderungen wirken sofort — die LLM wird beim nächsten Analyse-Start mit diesen Werten instruiert.",
            bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8),
        ).pack(fill="x", padx=12, pady=(0, 6))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=BTN_BG, foreground=FG_TEXT,
                        padding=[10, 4])
        style.map("TNotebook.Tab", background=[("selected", BG_PANEL)])

        for feld, label in _FELDER:
            frame = tk.Frame(nb, bg=BG_MAIN)
            nb.add(frame, text=f"  {label}  ")
            self._build_tab(frame, feld)

    def _build_tab(self, parent: tk.Frame, feld: str):
        # Treeview
        tree_frame = tk.Frame(parent, bg=BG_MAIN)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        style = ttk.Style()
        style.configure("Typ.Treeview",
                        background=BG_PANEL, foreground=FG_TEXT,
                        fieldbackground=BG_PANEL, rowheight=24,
                        font=("Segoe UI", 9))
        style.configure("Typ.Treeview.Heading",
                        background=BG_INPUT, foreground=ACCENT_LAVENDER,
                        font=("Segoe UI", 9, "bold"))
        style.map("Typ.Treeview", background=[("selected", "#3d3d5c")])

        col_ids = [c[0] for c in _COLS]
        tree = ttk.Treeview(tree_frame, columns=col_ids, show="headings",
                            style="Typ.Treeview", selectmode="browse")

        for key, header, width in _COLS:
            tree.heading(key, text=header)
            tree.column(key, width=width, minwidth=60, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        tree.tag_configure("retail",        foreground=ACCENT_GREEN,  background="#1a2e1a")
        tree.tag_configure("institutional", foreground=ACCENT_BLUE,   background="#1a1e2e")

        tree.bind("<Double-1>", lambda e, f=feld, t=tree: self._edit_selected(f, t))

        self._trees[feld] = tree

        # Button-Leiste
        btn_frame = tk.Frame(parent, bg=BG_MAIN)
        btn_frame.pack(fill="x", padx=6, pady=6)

        tk.Button(
            btn_frame, text="+ Hinzufügen",
            command=lambda f=feld, t=tree: self._add_entry(f, t),
            bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left")

        tk.Button(
            btn_frame, text="✏ Bearbeiten",
            command=lambda f=feld, t=tree: self._edit_selected(f, t),
            bg=BTN_BG, fg=ACCENT_BLUE, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left", padx=(4, 0))

        tk.Button(
            btn_frame, text="🗑 Löschen",
            command=lambda f=feld, t=tree: self._delete_selected(f, t),
            bg=BTN_BG, fg=ACCENT_RED, relief="flat",
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BTN_ACTIVE,
        ).pack(side="left", padx=(4, 0))

    # ─── Daten ───────────────────────────────────────────────────────────────

    def _refresh_all(self):
        for feld, _ in _FELDER:
            self._refresh_tree(feld)

    def _refresh_tree(self, feld: str):
        tree = self._trees[feld]
        tree.delete(*tree.get_children())
        for row in typologie_store.get_werte(feld):
            tag = row["segment"] if row["segment"] in ("retail", "institutional") else ""
            tree.insert("", "end", iid=str(row["id"]),
                        values=(row["wert"], row["segment"], row["synonyme"]),
                        tags=(tag,))

    # ─── Aktionen ────────────────────────────────────────────────────────────

    def _add_entry(self, feld: str, tree: ttk.Treeview):
        self._open_edit_dialog(feld, tree, None)

    def _edit_selected(self, feld: str, tree: ttk.Treeview):
        sel = tree.selection()
        if not sel:
            return
        self._open_edit_dialog(feld, tree, int(sel[0]))

    def _delete_selected(self, feld: str, tree: ttk.Treeview):
        sel = tree.selection()
        if not sel:
            return
        id_ = int(sel[0])
        vals = tree.item(sel[0], "values")
        if messagebox.askyesno(
            "Löschen", f'Eintrag "{vals[0]}" wirklich löschen?', parent=self
        ):
            typologie_store.delete_wert(id_)
            self._refresh_tree(feld)

    def _open_edit_dialog(self, feld: str, tree: ttk.Treeview, id_: int | None):
        is_new = id_ is None
        existing = {}
        if not is_new:
            vals = tree.item(str(id_), "values")
            existing = {"wert": vals[0], "segment": vals[1], "synonyme": vals[2]}

        dlg = tk.Toplevel(self)
        dlg.title("Hinzufügen" if is_new else "Bearbeiten")
        dlg.configure(bg=BG_MAIN)
        dlg.geometry("520x240")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        def row(label_text, widget_factory, pady=4):
            f = tk.Frame(dlg, bg=BG_MAIN)
            f.pack(fill="x", padx=16, pady=pady)
            tk.Label(f, text=label_text, bg=BG_MAIN, fg=FG_MUTED,
                     font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
            w = widget_factory(f)
            w.pack(side="left", fill="x", expand=True)
            return w

        var_wert    = tk.StringVar(value=existing.get("wert", ""))
        var_segment = tk.StringVar(value=existing.get("segment", ""))
        var_syn     = tk.StringVar(value=existing.get("synonyme", ""))

        tk.Label(dlg, text=f"Feld: {feld}", bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(10, 0))

        row("Wert / Ausprägung",
            lambda p: tk.Entry(p, textvariable=var_wert, bg=BG_INPUT, fg=FG_TEXT,
                               insertbackground=FG_TEXT, relief="flat",
                               font=("Segoe UI", 9)))

        def seg_cb(parent):
            cb = ttk.Combobox(parent, textvariable=var_segment,
                              values=_SEGMENT_OPTS, state="readonly",
                              font=("Segoe UI", 9), width=18)
            return cb
        row("Segment", seg_cb)

        row("Synonyme / EN",
            lambda p: tk.Entry(p, textvariable=var_syn, bg=BG_INPUT, fg=FG_TEXT,
                               insertbackground=FG_TEXT, relief="flat",
                               font=("Segoe UI", 9)))

        btn_f = tk.Frame(dlg, bg=BG_MAIN)
        btn_f.pack(fill="x", padx=16, pady=(12, 0))

        def _save():
            wert = var_wert.get().strip()
            if not wert:
                messagebox.showwarning("Pflichtfeld", "Wert darf nicht leer sein.", parent=dlg)
                return
            if is_new:
                ok = typologie_store.add_wert(feld, wert, var_segment.get(), var_syn.get())
                if not ok:
                    messagebox.showwarning("Duplikat",
                                           f'"{wert}" existiert bereits in {feld}.', parent=dlg)
                    return
            else:
                typologie_store.update_wert(id_, wert, var_segment.get(), var_syn.get())
            dlg.destroy()
            self._refresh_tree(feld)

        tk.Button(btn_f, text="Speichern", command=_save,
                  bg=BTN_BG, fg=ACCENT_GREEN, relief="flat",
                  font=("Segoe UI", 9), padx=12, pady=3, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left")
        tk.Button(btn_f, text="Abbrechen", command=dlg.destroy,
                  bg=BTN_BG, fg=FG_MUTED, relief="flat",
                  font=("Segoe UI", 9), padx=12, pady=3, cursor="hand2",
                  activebackground=BTN_ACTIVE).pack(side="left", padx=(6, 0))
