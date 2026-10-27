#!/usr/bin/env python3
"""
arc_peq.py - talk to the Audiocular Arc's built-in 5-band PEQ over USB HID.

Protocol (decoded from the official PEQ editor, report ID 0x4B, 10 data bytes):
    [cmd, 0, 0, 0, rw, 0, p0, p1, p2, p3]
    rw   = 0x52 'R' (read) | 0x57 'W' (write)
    Band i (0..4) uses two commands:
        0x26 + 2i : p = gain(int16 LE, 0.1 dB units), freq(uint16 LE, Hz)
        0x27 + 2i : p = Q(uint16 LE, Q * 1000),       type(uint16 LE)
    Types seen: 8 = peak, 11 = low shelf, 12 = high shelf
    A write is acknowledged with payload 03 00 00 00.
    The editor wraps a write as: W 0x02=0x65, band writes, W 0x24=0x01, W 0x02=0x65
    (the meaning of 0x02 / 0x24 is inferred, not confirmed).
    DAC volume: W cmd 0x3b, payload step (0x09..0x0f = -6..+3 dB, 0x0d = 0 dB).
    Persistence: after the apply sequence the editor sends rw 0x53 ('S') for 0x26, 0x27, 0x28 with the band's
    own bytes; only the first is acknowledged. --save sends just that first one (experimental).

Usage (macOS):
    pip3 install hidapi
    python3 arc_peq.py list
    python3 arc_peq.py read  [--vid 0x.... --pid 0x....]
    python3 arc_peq.py write --preset bass_vocals_v1 --yes
"""
import argparse, struct, sys, time

REPORT_ID = 0x4B
TYPES = {"peak": 8, "lowshelf": 11, "highshelf": 12}
TYPE_NAMES = {v: k for k, v in TYPES.items()}

# (type, freq Hz, Q, gain dB)
PRESETS = {
    "bass_vocals_v1": [
        ("lowshelf", 90, 0.7, 4.5),
        ("peak", 250, 1.0, -2.0),
        ("peak", 2500, 1.2, 2.5),
        ("peak", 6000, 2.0, -2.0),
        ("highshelf", 10000, 0.7, 1.5),
    ],
    "flat": [
        ("lowshelf", 90, 0.7, 0.0),
        ("peak", 250, 1.0, 0.0),
        ("peak", 2500, 1.0, 0.0),
        ("peak", 6000, 1.0, 0.0),
        ("highshelf", 10000, 0.7, 0.0),
    ],
}


def packet(cmd, rw, payload=b"\0\0\0\0"):
    return bytes([REPORT_ID, cmd, 0, 0, 0, rw, 0]) + bytes(payload)


def parse(resp):
    resp = bytes(resp)
    if resp and resp[0] != REPORT_ID:  # some stacks strip the report ID
        resp = bytes([REPORT_ID]) + resp
    return resp[1], resp[5], resp[7:11]


def encode_band(i, band):
    typ, freq, q, gain = band
    a = struct.pack("<hH", round(gain * 10), int(freq))
    b = struct.pack("<HH", round(q * 1000), TYPES[typ])
    return [(0x26 + 2 * i, a), (0x27 + 2 * i, b)]


def decode_band(a, b):
    gain, freq = struct.unpack("<hH", a)
    q, typ = struct.unpack("<HH", b)
    return (TYPE_NAMES.get(typ, f"type{typ}"), freq, q / 1000, gain / 10)


class Arc:
    def __init__(self, dev):
        self.dev = dev

    def xfer(self, cmd, rw, payload=b"\0\0\0\0", timeout=500):
        self.dev.write(packet(cmd, rw, payload))
        end = time.time() + timeout / 1000
        while time.time() < end:
            r = self.dev.read(64, 50)
            if r:
                c, w, p = parse(r)
                if c == cmd and w == rw:
                    return p
        raise TimeoutError(f"no reply to cmd 0x{cmd:02x}")

    def version(self):
        return (self.xfer(4, 0x52)[:4] + self.xfer(5, 0x52)[:1]).split(b"\0")[0].decode(errors="replace")

    def read_bands(self):
        out = []
        for i in range(5):
            a = self.xfer(0x26 + 2 * i, 0x52)
            b = self.xfer(0x27 + 2 * i, 0x52)
            out.append(decode_band(a, b))
        return out

    def save(self):
        """Experimental: send 'S' (0x53) for cmd 0x26 with Band 1's current bytes, like the official editor."""
        cur = self.xfer(0x26, 0x52)
        ack = self.xfer(0x26, 0x53, cur)
        if ack[0] != 3:
            raise RuntimeError(f"save not acknowledged: {ack.hex()}")

    def write_bands(self, bands):
        def w(cmd, p):
            ack = self.xfer(cmd, 0x57, p)
            if ack[0] != 3:
                raise RuntimeError(f"cmd 0x{cmd:02x} not acknowledged: {ack.hex()}")
        w(0x02, bytes([0x65, 0, 0, 0]))
        for i, band in enumerate(bands):
            for cmd, p in encode_band(i, band):
                w(cmd, p)
        w(0x24, bytes([1, 0, 0, 0]))
        w(0x02, bytes([0x65, 0, 0, 0]))


def open_arc(hid, vid, pid):
    cands = [d for d in hid.enumerate()
             if (vid is None or d["vendor_id"] == vid) and (pid is None or d["product_id"] == pid)
             and (vid or pid or "arc" in (d.get("product_string") or "").lower()
                  or "audiocular" in (d.get("manufacturer_string") or "").lower())]
    for d in cands:  # several HID interfaces exist; keep the one that answers our probe
        dev = hid.device()
        try:
            dev.open_path(d["path"])
            dev.set_nonblocking(False)
            arc = Arc(dev)
            arc.version()
            return arc
        except Exception:
            try:
                dev.close()
            except Exception:
                pass
    sys.exit("Could not find an Arc interface that answers. Try 'list' and pass --vid/--pid.")


def show(bands):
    print(f"{'Band':<5}{'Type':<11}{'Freq':>7}{'Q':>7}{'Gain':>8}")
    for i, (t, f, q, g) in enumerate(bands, 1):
        print(f"{i:<5}{t:<11}{f:>7}{q:>7.2f}{g:>+8.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "read", "write", "probe"])
    ap.add_argument("--vid", type=lambda x: int(x, 0))
    ap.add_argument("--pid", type=lambda x: int(x, 0))
    ap.add_argument("--preset", choices=PRESETS, default="bass_vocals_v1")
    ap.add_argument("--save", action="store_true", help="after writing, also send the editor's 'S' save command")
    ap.add_argument("--yes", action="store_true", help="really write to the device")
    a = ap.parse_args()
    import hid
    if a.cmd == "list":
        for d in hid.enumerate():
            print(f"{d['vendor_id']:#06x}:{d['product_id']:#06x} usage_page={d['usage_page']:#x} "
                  f"{d.get('manufacturer_string')} / {d.get('product_string')}")
        return
    if (a.vid is None) != (a.pid is None):
        sys.exit("Pass both --vid and --pid, or neither.")
    arc = open_arc(hid, a.vid, a.pid)
    print("Firmware:", arc.version())
    if a.cmd == "probe":   # read-only: ask what the unexplained editor commands report; nothing is written
        for cmd in (0x02, 0x24):
            try:
                print(f"read 0x{cmd:02x}:", arc.xfer(cmd, 0x52).hex(" "))
            except TimeoutError as e:
                print(f"read 0x{cmd:02x}: no reply ({e})")
        return
    if a.cmd == "read":
        show(arc.read_bands())
    else:
        print("Current:"); show(arc.read_bands())
        print(f"\nPreset '{a.preset}':"); show(PRESETS[a.preset])
        if not a.yes:
            sys.exit("\nDry run. Add --yes to write this to the device.")
        arc.write_bands([(t, f, q, g) for t, f, q, g in PRESETS[a.preset]])
        print("\nWritten. Read back:"); show(arc.read_bands())
        if a.save:
            arc.save(); print("\nSave command acknowledged. Now unplug, replug and run 'read'.")


if __name__ == "__main__":
    main()
