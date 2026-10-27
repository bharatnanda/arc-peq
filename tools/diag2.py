#!/usr/bin/env python3
"""Runs the real app window against the real Arc for ~6 s (no clicks needed), then reports what happened. Writes nothing
to the device. Usage (from the app folder, app + web editor closed):
    python3 tools/diag2.py offscreen          # app logic, no real window
    python3 tools/diag2.py cocoa              # real window (opens and closes by itself)
    python3 tools/diag2.py cocoa norescan     # real window, background device scanning switched off
"""
import logging, os, sys
mode = sys.argv[1] if len(sys.argv) > 1 else "cocoa"
if mode == "offscreen":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="   log: %(message)s")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from arcpeq.app import MainWindow, STYLE
from arcpeq.device import RealBackend

app = QApplication([sys.argv[0]])
app.setStyleSheet(STYLE)
w = MainWindow(RealBackend())
if "norescan" in sys.argv:
    w.ctl.timer.stop()
w.show()

def report():
    print(f"mode={' '.join(sys.argv[1:]) or 'cocoa'}")
    print("  device selected :", w.current.label if w.current else None)
    print("  firmware        :", w.firmware or None)
    print("  bands read      :", w.device_bands is not None)
    print("  status message  :", w.message.text() or "(none)")
    w.device_bands = list(w.bands)
    w.close()
    app.quit()

QTimer.singleShot(6000, report)
app.exec()
