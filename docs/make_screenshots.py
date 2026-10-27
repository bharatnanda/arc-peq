"""Regenerates docs/images/*.png and demo.gif from the demo backend (no hardware needed).
Run from the project root:  QT_QPA_PLATFORM=offscreen python docs/make_screenshots.py   (needs Pillow)"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
mock.patch.object(Path, "home", return_value=Path(tempfile.mkdtemp())).start()   # keep the real ~/.arcpeq untouched

from PIL import Image                                   # noqa: E402
from PySide6.QtCore import QPoint, Qt                   # noqa: E402
from PySide6.QtGui import QImage                       # noqa: E402
from PySide6.QtTest import QTest                        # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

from arcpeq.app import STYLE, MainWindow                # noqa: E402
from arcpeq.core import LIBRARY                         # noqa: E402
from arcpeq.device import DemoBackend                   # noqa: E402

OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setStyleSheet(STYLE)


def pump(s):
    end = time.time() + s
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


MainWindow.msg = lambda self, *a, **k: True
w = MainWindow(DemoBackend(), demo=True)
w.resize(1240, 800)
w.show()
pump(2.6)
preset = {p.id: p for p in LIBRARY}
frames = []


def shot(name=None, hold=1):
    pump(0.08)
    img = w.grab()
    if name:
        img.save(str(OUT / name))
    for _ in range(hold):
        frames.append(img.toImage().copy())


def pick(pid):
    w.load_preset(preset[pid])


# --- screenshots -------------------------------------------------------------------------------
w.dismiss_intro()
pick("balanced")
shot("main.png")
pick("gaming")
w.vol.setValue(11)
pump(0.2)
shot("gaming-level.png")
pick("bass")
w.vol.setValue(13)
w.on_band_selected(0)
shot("bass.png")

# --- animated demo -----------------------------------------------------------------------------
frames.clear()
pick("balanced")
w.vol.setValue(13)
shot(hold=9)
pick("gaming")
shot(hold=9)
pick("balanced")
w.on_band_selected(3)
shot(hold=4)
c = w.curve
r = c.plot()
p0 = c.handle_pos(3, r).toPoint()
QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, p0)
for k in range(1, 15):                                  # drag band 4 up and to the right
    QTest.mouseMove(c, QPoint(p0.x() + 5 * k, p0.y() - 4 * k))
    shot()
QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(p0.x() + 70, p0.y() - 56))
shot(hold=6)
for v in (12, 11, 10, 9):                               # lower the output level; the whole curve follows
    w.vol.setValue(v)
    shot(hold=3)
shot(hold=10)
w.device_bands = list(w.bands)                          # so closing doesn't ask about unsaved changes
w.close()

scale = 900 / frames[0].width()
imgs = []
for f in frames:
    f = f.convertToFormat(QImage.Format_RGBA8888)
    im = Image.frombuffer("RGBA", (f.width(), f.height()), bytes(f.constBits()), "raw", "RGBA", f.bytesPerLine(), 1)
    imgs.append(im.convert("RGB").resize((900, int(f.height() * scale)), Image.LANCZOS))
imgs[0].save(OUT / "demo.gif", save_all=True, append_images=imgs[1:], duration=110, loop=0, optimize=True)
print("wrote", sorted(p.name for p in OUT.iterdir()), len(imgs), "frames")
