"""Writes icon.png / icon.ico / icon.icns from the in-code logo. Run: python packaging/make_icons.py"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtGui import QGuiApplication  # noqa: E402

from arcpeq.logo import logo_image  # noqa: E402

app = QGuiApplication([])
out = Path(__file__).resolve().parent
logo_image(1024).save(str(out / "icon.png"))
try:
    from PIL import Image
    im = Image.open(out / "icon.png").convert("RGBA")
    im.save(out / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    im.save(out / "icon.icns")
    print("wrote icon.png, icon.ico, icon.icns")
except ImportError:
    print("wrote icon.png (install Pillow for .ico/.icns:  pip install pillow)")
