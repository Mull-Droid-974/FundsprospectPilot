"""
Fondsstruktur-Fenster — hierarchische, Morningstar-angereicherte, geprüfte Ansicht.

Zeigt Umbrella (Branding) → Portfolio (Subfonds) → Anteilsklasse mit Identifiern,
Bezeichnungen, MS-Anreicherung und Prüf-Status (🟢/🟡/🔴).
Datenquelle: struktur_store.build_structure().
"""

import tkinter as tk
from tkinter import ttk

import struktur_store as ss

BG_MAIN = "#1e1e2e"; BG_PANEL = "#2a2a3e"; BG_INPUT = "#313145"
FG_TEXT = "#cdd6f4"; FG_MUTED = "#7f849c"
ACCENT_LAVENDER = "#b4befe"; ACCENT_GREEN = "#a6e3a1"
ACCENT_YELLOW = "#f9e2af"; ACCENT_RED = "#f38ba8"
BTN_BG = "#45475a"; BTN_ACTIVE = "#585b70"

_CHECK_ICON = {ss.OK: "🟢", ss.WARN: "🟡", ss.ERROR: "🔴"}

# Spalten (neben der Baum-Spalte #0)
_COLS = [
    ("isin",         "ISIN",         120, "w"),
    ("secid",        "SecId",        95,  "w"),
    ("kategorie",    "Kategorie",    190, "w"),
    ("status",       "Status",       70,  "w"),
    ("domizil",      "Domizil",      90,  "w"),
    ("waehrung",     "Währung",      65,  "w"),
    ("ter",          "TER",          60,  "e"),
    ("rating",       "Rating",       55,  "e"),
    ("pruefung",     "Prüfung",      260, "w"),
]


class StrukturWindow(tk.Toplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Fondsstruktur — Umbrella / Portfolio / Anteilsklasse")
        self.geometry("1400x800")
        self.configure(bg=BG_MAIN)
        self._tree_data = []
        self._build_ui()
        self.refresh()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = tk.Frame(self, bg=BG_PANEL); bar.pack(fill="x")
        inner = tk.Frame(bar, bg=BG_PANEL); inner.pack(fill="x", padx=12, pady=8)
        tk.Label(inner, text="Fondsstruktur", bg=BG_PANEL, fg=ACCENT_LAVENDER,
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(inner, text="  🟢 OK   🟡 Warnung   🔴 Fehler",
                 bg=BG_PANEL, fg=FG_MUTED, font=("Segoe UI", 9)).pack(side="left", padx=12)
        for txt, cmd in (("Aktualisieren", self.refresh),
                         ("Alle einklappen", lambda: self._expand_all(False)),
                         ("Alle ausklappen", lambda: self._expand_all(True))):
            tk.Button(inner, text=txt, command=cmd, bg=BTN_BG, fg=FG_TEXT, relief="flat",
                      font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
                      activebackground=BTN_ACTIVE).pack(side="right", padx=(4, 0))

        sb = tk.Frame(self, bg=BG_MAIN); sb.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(sb, text="🔍", bg=BG_MAIN, fg=FG_MUTED).pack(side="left")
        self._search = tk.StringVar()
        self._search.trace_add("write", lambda *_: self._fill())
        tk.Entry(sb, textvariable=self._search, bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=("Segoe UI", 10)
                 ).pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=4)
        self._stats = tk.StringVar()
        tk.Label(sb, textvariable=self._stats, bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(8, 0))

        tf = tk.Frame(self, bg=BG_MAIN); tf.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        style = ttk.Style(); style.theme_use("clam")
        style.configure("Struk.Treeview", background=BG_PANEL, foreground=FG_TEXT,
                        fieldbackground=BG_PANEL, rowheight=24, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Struk.Treeview.Heading", background=BG_INPUT, foreground=ACCENT_LAVENDER,
                        relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Struk.Treeview", background=[("selected", "#3d3d5c")])

        self._tree = ttk.Treeview(tf, columns=[c[0] for c in _COLS], show="tree headings",
                                  style="Struk.Treeview", selectmode="browse")
        self._tree.heading("#0", text="Umbrella / Portfolio / Anteilsklasse")
        self._tree.column("#0", width=420, minwidth=200, anchor="w")
        for key, header, width, anchor in _COLS:
            self._tree.heading(key, text=header)
            self._tree.column(key, width=width, minwidth=40, anchor=anchor)
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tf.rowconfigure(0, weight=1); tf.columnconfigure(0, weight=1)

        self._tree.tag_configure("error", foreground=ACCENT_RED)
        self._tree.tag_configure("warn", foreground=ACCENT_YELLOW)
        self._tree.tag_configure("umbrella", background="#26263a", font=("Segoe UI", 9, "bold"))
        self._tree.tag_configure("subfonds", background="#22222f")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        self._detail = tk.Text(self, height=3, bg=BG_INPUT, fg=FG_TEXT, relief="flat",
                               font=("Segoe UI", 9), wrap="word")
        self._detail.pack(fill="x", padx=12, pady=(0, 8))
        self._detail.configure(state="disabled")

    # ── Daten ─────────────────────────────────────────────────────────────────
    def refresh(self):
        self._stats.set("lädt …"); self.update_idletasks()
        self._tree_data = ss.build_structure()
        s = ss.structure_stats(self._tree_data)
        ks = s["klassen_status"]
        self._stats.set(f"{s['umbrellas']} Umbrellas · {s['subfonds']} Subfonds · "
                        f"{s['klassen']} Klassen  |  🟢 {ks['ok']} 🟡 {ks['warn']} 🔴 {ks['error']}")
        self._fill()

    def _fill(self):
        self._tree.delete(*self._tree.get_children())
        q = self._search.get().strip().lower()
        for u in self._tree_data:
            subs = []
            for s in u["subfonds"]:
                kl = [k for k in s["anteilsklassen"]
                      if not q or q in k["isin"].lower() or q in (k["anteilsklasse"] or "").lower()
                      or q in (s["subfonds_name"] or "").lower() or q in (u["umbrella_name"] or "").lower()
                      or q in (k["kategorie"] or "").lower()]
                if kl:
                    subs.append((s, kl))
            if not subs:
                continue
            uid = self._tree.insert("", "end", text=f"{_CHECK_ICON[u['check']]} {u['umbrella_name']}",
                                    values=("", "", "", "", "", "", "", "",
                                            f"{u['n_subfonds']} Subfonds / {u['n_klassen']} Klassen"),
                                    open=bool(q), tags=("umbrella",))
            for s, kl in subs:
                sid = self._tree.insert(uid, "end",
                                        text=f"{_CHECK_ICON[s['check']]}   {s['subfonds_name']}",
                                        values=("", "", "", "", "", "", "", "", f"{s['n_klassen']} Klassen"),
                                        open=bool(q), tags=("subfonds",))
                for k in kl:
                    tags = ("klasse",)
                    if k["check"] == ss.ERROR: tags = ("error",)
                    elif k["check"] == ss.WARN: tags = ("warn",)
                    self._tree.insert(
                        sid, "end",
                        text=f"{_CHECK_ICON[k['check']]}      {k['anteilsklasse'] or '(ohne Bez.)'}",
                        values=(k["isin"], k["secid"], k["kategorie"], k["status"], k["domizil"],
                                k["waehrung"], k["ter"], k["rating"],
                                "; ".join(k["issues"]) if k["issues"] else ""),
                        tags=tags)

    def _expand_all(self, state):
        def rec(node):
            self._tree.item(node, open=state)
            for c in self._tree.get_children(node):
                rec(c)
        for n in self._tree.get_children():
            rec(n)

    def _on_select(self, _e=None):
        sel = self._tree.focus()
        if not sel:
            return
        vals = self._tree.item(sel, "values")
        txt = self._tree.item(sel, "text")
        info = f"{txt}"
        if vals and vals[0]:
            info = (f"{txt}\nISIN: {vals[0]}   SecId: {vals[1]}   Kategorie: {vals[2]}   "
                    f"Status: {vals[3]}   Domizil: {vals[4]}   Währung: {vals[5]}   "
                    f"TER: {vals[6]}   Rating: {vals[7]}")
            if vals[8]:
                info += f"\n⚠ Prüfung: {vals[8]}"
        self._detail.configure(state="normal")
        self._detail.delete("1.0", "end"); self._detail.insert("1.0", info)
        self._detail.configure(state="disabled")
