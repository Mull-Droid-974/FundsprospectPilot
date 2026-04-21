"""
FundProspektDownloader — Standalone-Einstiegspunkt.

Liest ISINs aus der konfigurierten results.db und lädt Verkaufsprospekte
von fundinfo.com herunter. Starten mit: python main.py
"""

import os
import sys
import tkinter as tk
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from download_window import DownloadWindow

if __name__ == "__main__":
    db_path = os.getenv("DB_PATH", "")
    pdf_folder = Path(os.getenv("PDF_FOLDER", str(Path(__file__).parent / "data" / "prospekte")))

    if db_path:
        import results_store_ext
        results_store_ext.set_db_path(db_path)

    root = tk.Tk()
    root.withdraw()

    win = DownloadWindow(root, pdf_folder=pdf_folder)
    win.protocol("WM_DELETE_WINDOW", root.destroy)

    root.mainloop()
