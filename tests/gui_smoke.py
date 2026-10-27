import os, sys, time, tempfile
os.environ["QT_QPA_PLATFORM"]="offscreen"
from pathlib import Path
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__)))+"")
tmp=tempfile.mkdtemp(); mock.patch.object(Path,"home",return_value=Path(tmp)).start()
from PySide6.QtWidgets import QApplication
from arcpeq.core import *
from arcpeq.device import DemoBackend, DeviceError
from arcpeq.app import MainWindow, STYLE
app=QApplication([]); app.setStyleSheet(STYLE)
def pump(s):
    t=time.time()
    while time.time()-t<s: app.processEvents(); time.sleep(0.01)
asked=[]; ans=[False]
MainWindow.msg=lambda self,t,x,*a,**k:(asked.append(t) or ans[0])
be=DemoBackend(); sent=[]
orig=be.open
def spy(info,*a):
    s=orig(info,*a); ow=s.dev.write
    def w(d): sent.append(bytes(d[:8]).hex(" ")); return ow(d)
    s.dev.write=w; return s
be.open=spy
w=MainWindow(be,demo=True); w.ctl.settle=0.2; w.show(); pump(2.5)
assert w.current and w.current.verified, "auto-selected verified"
# switch to unverified device: nothing auto, gate on volume
b=[d for d in w.devs if not d.verified][0]
w.select_device(b); pump(1.0); sent.clear()
w.vol.setValue(10); pump(0.3); w.apply(); pump(0.6)
print("asked:",asked,"| volume writes:",[x for x in sent if x.startswith("4b 3b")])
assert "Send settings to this device?" in asked and not any(x.startswith("4b 3b") for x in sent)
# apply refused -> no writes
w.load_preset(LIBRARY[1]); sent.clear(); w.apply(); pump(0.5)
assert not [x for x in sent if x.startswith("4b 26")]
# apply failure path
ans[0]=True
sess_open=be.open
def failing(info,*a):
    s=sess_open(info,*a); 
    def bad(*x,**k): raise DeviceError("boom")
    s.write_bands=bad; return s
be.open=failing
w.apply(); pump(0.15)
print("uncertain:",w.current.key in w.uncertain,"|",w.message.text()[:60])
assert w.current.key in w.uncertain
be.open=spy; pump(1.0)
print("after reread uncertain:",w.current.key in w.uncertain)
# graph follows the output level
w.vol.setValue(15); pump(0.1)
assert w.curve.level == 3.0, w.curve.level
w.vol.setValue(9); pump(0.1)
assert w.curve.level == -6.0, w.curve.level
w.close(); print("OK")
