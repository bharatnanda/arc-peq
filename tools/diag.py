#!/usr/bin/env python3
"""Read-only diagnostic: finds out which way of talking to the Arc fails on this computer. Writes nothing.
Run from the app folder:  python3 tools/diag.py     Close the web editor and the app first."""
import os, sys, time, threading, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hid
from arcpeq import device as D

def devs():
    return [d for d in hid.enumerate() if d["vendor_id"] == 0x3302 and d["product_id"] == 0x20E9]

def show():
    for d in devs():
        print("  iface", d.get("interface_number"), "usage_page", hex(d["usage_page"]), "usage", d["usage"], d["path"])

def run(name, fn):
    t0 = time.time()
    try:
        fn()
        print(f"PASS  {name}  ({time.time()-t0:.2f}s)")
    except Exception as e:                           # noqa: BLE001
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
    time.sleep(0.7)

def opened(exclusive=None):
    ds = devs()
    if not ds:
        raise SystemExit("Arc not found")
    dev = hid.device()
    if exclusive is not None and hasattr(hid, "hid_darwin_set_open_exclusive"):
        hid.hid_darwin_set_open_exclusive(exclusive)
    dev.open_path(ds[0]["path"])
    dev.set_nonblocking(False)
    return D.Session(dev)

def plain(drain, enum_between=False, exclusive=None, threaded=False):
    def go():
        s = opened(exclusive)
        try:
            if not drain:
                s._drain = lambda: None
            s.version()
            if enum_between:
                orig = s.xfer
                def x(*a, **k):
                    hid.enumerate(); return orig(*a, **k)
                s.xfer = x
            assert len(s.read_bands()) == 5
        finally:
            s.close()
    if not threaded:
        return go
    def th():
        err = []
        t = threading.Thread(target=lambda: (lambda: go())() if True else None)
        def w():
            try: go()
            except Exception as e: err.append(e)
        t = threading.Thread(target=w); t.start(); t.join()
        if err: raise err[0]
    return th

print("Devices seen:"); show()
print("hidapi:", getattr(hid, "__version__", "?"), "| has exclusive switch:", hasattr(hid, "hid_darwin_set_open_exclusive"))
run("A  like the CLI (no drain)", plain(False))
run("B  with drain (current app)", plain(True))
run("C  drain + hid.enumerate() between commands (what the 1.2 s rescan does)", plain(True, enum_between=True))
run("D  drain, in a worker thread (what the app does)", plain(True, threaded=True))
run("E  no drain, in a worker thread", plain(False, threaded=True))
if hasattr(hid, "hid_darwin_set_open_exclusive"):
    run("F  drain, NON-exclusive open", plain(True, exclusive=0))
    run("G  drain, exclusive open", plain(True, exclusive=1))
print("Done. Paste everything above.")
