import os, sys, time, tempfile
os.environ["QT_QPA_PLATFORM"]="offscreen"
from pathlib import Path
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp=tempfile.mkdtemp(); mock.patch.object(Path,"home",return_value=Path(tmp)).start()
from PySide6.QtWidgets import QApplication, QSlider
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from arcpeq.device import DemoBackend
from arcpeq.app import MainWindow, STYLE
app=QApplication([]); app.setStyleSheet(STYLE)
def pump(s):
    t=time.time()
    while time.time()-t<s: app.processEvents(); time.sleep(0.01)
MainWindow.msg=lambda self,*a,**k:True
w=MainWindow(DemoBackend(),demo=True); w.show(); pump(2.5)
from PySide6.QtWidgets import QStyle, QStyleOptionSlider
def handle_center(sl):
    o=QStyleOptionSlider(); sl.initStyleOption(o)
    return sl.style().subControlRect(QStyle.CC_Slider,o,QStyle.SC_SliderHandle,sl).center()
def drag(sl, frac):
    h=handle_center(sl); r=sl.rect(); y=h.y()
    x1=int(r.left()+r.width()*frac)
    QTest.mousePress(sl,Qt.LeftButton,Qt.NoModifier,h)
    for k in range(1,11):
        QTest.mouseMove(sl,QPoint(h.x()+(x1-h.x())*k//10,y)); app.processEvents()
    QTest.mouseRelease(sl,Qt.LeftButton,Qt.NoModifier,QPoint(x1,y)); pump(0.4)
bad=0
def check(name,sl,frac):
    global bad
    b=sl.value(); drag(sl,frac); a=sl.value()
    ok=(a>b) if frac>0.5 else (a<b)
    bad+=not ok
    print(f"{'OK ' if ok else 'BAD'} {name}: {b} -> {a}")
print("vol enabled:",w.vol.isEnabled(),"busy:",w.busy)
check("output level right",w.vol,0.95); print("   label",w.vol_label.text())
check("output level left",w.vol,0.05); print("   label",w.vol_label.text())
for i,c in enumerate(w.cards):
    check(f"band{i+1} gain right",c.gain,0.9); check(f"band{i+1} gain left",c.gain,0.1)
    check(f"band{i+1} freq right",c.freq_slider,0.9); check(f"band{i+1} freq left",c.freq_slider,0.1)
# did the volume reach the device?
print("volume now:",w.volume, "| failures:",bad)
w.device_bands=list(w.bands); w.close(); sys.exit(1 if bad else 0)
