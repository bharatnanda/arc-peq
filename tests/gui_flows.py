"""End-to-end UI flows against the simulated Arc (offscreen). Run:  QT_QPA_PLATFORM=offscreen python3 tests/gui_flows.py"""
import dataclasses, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp(); mock.patch.object(Path, "home", return_value=Path(tmp)).start()
from PySide6.QtWidgets import QApplication
from arcpeq.core import FLAT, LIBRARY, PEAK, LOW_SHELF, Band, volume_step, suggest_volume_db, peak_boost
from arcpeq.device import DemoBackend, DeviceError, Session
from arcpeq.app import MainWindow, STYLE

app = QApplication([]); app.setStyleSheet(STYLE)
def pump(s):
    t = time.time()
    while time.time() - t < s: app.processEvents(); time.sleep(0.01)
def wait(cond, timeout=12.0, what=""):
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents(); time.sleep(0.01)
        if cond(): return
    raise AssertionError("timed out waiting for " + what)
def idle(w): wait(lambda: not w.busy and w.job is None, 30, "idle")
def lib(id_): return next(p for p in LIBRARY if p.id == id_)

MainWindow.msg = lambda self, *a, **k: True
MainWindow.choose = lambda self, *a, **k: 1
notes = []
MainWindow.note = lambda self, t, x: notes.append(t)

def fresh(**store):
    be = DemoBackend(); be.stores["a"].update(store)
    w = MainWindow(be, demo=True); w.ctl.settle = 0.2; w.show()
    wait(lambda: w.current is not None and w.device_bands is not None, 10, "device read")
    return be, w
def key(w): return w.current.key
def done(w):
    w.device_bands = list(w.bands); w.close()      # nothing unsaved, so no 'Unsaved changes' dialog

# 1. live Apply: no restart, no save, level sent when staged
be, w = fresh()
w.load_preset(lib("bass")); w.vol.setValue(14); pump(0.1)
assert w.apply_btn.isEnabled()
w.apply(); idle(w)
assert w.device_bands == w.bands, "read back matches"
assert key(w) in w.unsaved and "Live" in w.pill.text(), w.pill.text()
assert w.volume.get(key(w)) == 14 and be.stores["a"]["vol"] == 14
assert any(d.key == key(w) for d in be.list_devices()), "live apply must not restart the device"
assert w.store_btn.isEnabled(), "Save to Arc is offered after a live apply"
print("1 live apply OK")

# 2. Save: restart, level put back, pill + message
w.save_to_device()
wait(lambda: w.busy, 3, "busy"); pump(0.1)
assert "Saving" in w.pill.text() and w.progress.isVisible() and not w.cards[0].isEnabled() and not w.vol.isEnabled()
idle(w)
assert be.stores["a"]["vol"] == 14, "level restored after the restart"
assert w.volume.get(key(w)) == 14 and key(w) in w.saved_keys and key(w) not in w.unsaved
assert "Saved" in w.pill.text() and "put back" in w.message.text(), (w.pill.text(), w.message.text())
assert not w.store_btn.isEnabled(), "nothing left to save"
print("2 save + restore level OK")
done(w)

# 3. Save with the level never set: shown as Unknown, message says so
be, w = fresh()
w.load_preset(lib("vocal")); w.save_to_device(); idle(w)
assert w.vol_label.text() == "Unknown" and "reset" in w.message.text(), (w.vol_label.text(), w.message.text())
print("3 unknown level shown OK")
done(w)

# 4. Restart starts late: the level must be restored AFTER the restart, not before it
be, w = fresh(restart_delay=1.5)
w.vol.setValue(14); w.apply(); idle(w)
log = []
orig = be.stores["a"]
import arcpeq.device as D
ow = D._FakeDev.write
def spy(self, data):
    if data[1] == 0x3B and data[5] == 0x57: log.append((time.time(), data[7]))
    return ow(self, data)
D._FakeDev.write = spy
w.load_preset(lib("cinema")); w.save_to_device(); idle(w)
D._FakeDev.write = ow
assert be.stores["a"]["vol"] == 14
assert log and log[-1][0] >= orig["gone_until"] - 0.05, "level restore happened before the restart finished"
print("4 late restart OK")
done(w)

# 5. Arc never restarts: not claimed as saved, level kept
be, w = fresh(no_restart=True)
w.vol.setValue(14); w.apply(); idle(w)
w.load_preset(lib("rock")); w.save_to_device(); idle(w)
assert "didn't restart" in w.message.text() and key(w) in w.unsaved and key(w) not in w.saved_keys
assert w.volume.get(key(w)) == 14
print("5 no-restart honesty OK")
done(w)

# 6. Restore of the level fails: level shown as unknown, lasting error
be, w = fresh()
w.vol.setValue(14); w.apply(); idle(w)
real = Session.set_volume
def failing(self, step): raise DeviceError("boom")
Session.set_volume = failing
w.load_preset(lib("night")); w.save_to_device(); idle(w)
Session.set_volume = real
assert w.volume.get(key(w)) is None and w.vol_label.text() == "Unknown"
assert "couldn't be put back" in w.message.text(), w.message.text()
pump(0.5); assert "couldn't be put back" in w.message.text(), "error message must be sticky"
print("6 failed level restore OK")
done(w)

# 7. Replug (new paths): level and unsaved state forgotten, device re-read
be, w = fresh()
w.vol.setValue(14); w.apply(); idle(w)
be.stores["a2"] = be.stores["a"]
be.devices[0] = dataclasses.replace(be.devices[0], paths=("a2",))
wait(lambda: key(w) not in w.volume, 5, "level forgotten on replug")
assert key(w) not in w.unsaved and "reconnected" in w.message.text()
wait(lambda: w.device_bands is not None, 5, "re-read after replug")
print("7 replug forgets level OK")
done(w)

# 8. Staged level belongs to one device
be, w = fresh()
w.vol.setValue(15); pump(0.1); assert w.vol_dirty
other = next(d for d in w.devs if not d.verified)
w.select_device(other); pump(0.1)
assert not w.vol_dirty and w.vol_pending is None, "staged level must not follow you to another device"
print("8 staged level per device OK")
done(w)

# 9. Hard boost cap: refused, nothing sent
be, w = fresh(); sent = []
ow = D._FakeDev.write
D._FakeDev.write = lambda self, d: (sent.append(bytes(d)), ow(self, d))[1]
w.bands = [Band(PEAK, 2000, 1.0, 12.0) for _ in range(5)]; w.refresh_all()
notes.clear(); w.apply(); pump(0.3)
D._FakeDev.write = ow
assert notes == ["Too much boost"] and not [p for p in sent if p[5] == 0x57], (notes, len(sent))
print("9 boost cap OK")
done(w)

# 10. Very strong boost: "Apply and set safe level" stages and sends the level too
be, w = fresh()
w.bands = [Band(LOW_SHELF, 100, 0.7, 11.0)] + list(FLAT.bands[1:]); w.refresh_all()
MainWindow.choose = lambda self, *a, **k: 0
w.apply(); idle(w)
want = volume_step(suggest_volume_db(peak_boost(w.bands)))
assert be.stores["a"]["vol"] == want and w.volume.get(key(w)) == want, (be.stores["a"].get("vol"), want)
MainWindow.choose = lambda self, *a, **k: 1
print("10 safe-level option OK")
done(w)

# 11. A failed quiet (background) read does not put a red error on screen
be, w = fresh(); w.flash("hello"); real_open = be.open
be.open = lambda *a, **k: (_ for _ in ()).throw(DeviceError("nope"))
w.ctl.read(w.current, silent=True); pump(0.5)
be.open = real_open
assert w.message.text() == "hello" and not w.retry_btn.isVisible()
print("11 quiet read errors OK")
done(w)

# 12. Closing the window mid-save returns promptly (does not wait out the restart)
be, w = fresh(restart_delay=4.0)
w.load_preset(lib("edm")); w.save_to_device(); wait(lambda: w.busy, 3, "busy"); pump(0.5)
t = time.time(); done(w)
assert time.time() - t < 3.0, f"close took {time.time()-t:.1f}s"
print("12 close during save OK")
print("ALL FLOWS OK")
