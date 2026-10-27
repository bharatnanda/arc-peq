"""USB access: device discovery, the protocol session, and a fake backend for --demo.

Safety rules enforced here:
  * Only devices the user can see and select are ever written to, and always by the exact path they picked.
  * Unknown devices are probed read-only, and only if their maker ID is known or the user added them.
  * Every value is validated before anything is sent, so a write is never left half-finished by bad input.
"""
from __future__ import annotations

import json
import platform
import logging
import re
import threading
import time
from dataclasses import dataclass, field

from .core import (MAX_BOOST_DB, NUM_BANDS, peak_boost, READ, VOL_MAX, VOL_MIN, WRITE, Band, FLAT, LIBRARY, clean_text, data_dir, decode_band,
                   encode_band, packet, parse_reply, read_small_file, validate_bands)

HID_LOCK = threading.RLock()          # hidapi is not guaranteed thread-safe: every call goes through this


class DeviceError(Exception):
    """Anything that went wrong talking to the device; the message is written for the user."""
    pass


class DeviceChanged(DeviceError):
    """The device answering is not the one the user selected."""


class Unsupported(DeviceError):
    """The device holds settings this app can't represent (read succeeded, decoding didn't)."""


@dataclass(frozen=True)
class Profile:
    """A device model the app knows: USB ids, name and the firmware versions it was verified against."""
    vid: int
    pid: int
    name: str
    firmwares: tuple = ()             # firmware versions this app has been verified against


PROFILES = [Profile(0x3302, 0x20E9, "Audiocular Arc", ("1.0.1",))]
KNOWN_MAKER_VIDS = {0x3302}           # same maker: probed read-only, flagged "unverified"


@dataclass(frozen=True)
class DeviceInfo:
    """One plugged-in device as the UI sees it (``verified`` = known model and firmware, so writes are allowed)."""
    key: str
    name: str
    vid: int
    pid: int
    serial: str
    verified: bool
    firmwares: tuple = ()
    paths: tuple = field(default=(), compare=False)
    ambiguous: bool = field(default=False, compare=False)     # two identical units we can't tell apart

    @property
    def label(self) -> str:
        tail = f" · {self.serial}" if self.serial else ""
        return f"{self.name}{tail}" + ("" if self.verified else "  (unverified)")


_UD_CACHE: dict = {"stamp": None, "value": []}


def _user_devices() -> list[tuple[int, int]]:
    """VID:PID pairs the user added. Strictly read (no symlinks/FIFOs), capped, and cached by mtime."""
    try:
        path = data_dir() / "devices.json"
        st = path.lstat()
        stamp = (st.st_mtime_ns, st.st_size)
        if _UD_CACHE["stamp"] == stamp:
            return _UD_CACHE["value"]
        raw = json.loads(read_small_file(path, 20_000).decode("utf-8"))
        out = []
        for v, p in raw[:50]:
            v, p = int(v), int(p)
            if 0 < v < 0x10000 and 0 < p < 0x10000:
                out.append((v, p))
        _UD_CACHE.update(stamp=stamp, value=out)
        return out
    except Exception:                                   # noqa: BLE001  (missing/hostile file: no extra devices)
        return []


def add_user_device(vid: int, pid: int) -> None:
    """Remember an extra VID:PID the user asked the app to look for."""
    from .core import atomic_write
    cur = _user_devices()
    if (vid, pid) not in cur:
        cur.append((vid, pid))
        atomic_write(data_dir() / "devices.json", json.dumps(cur))


def parse_vidpid(text: str) -> tuple[int, int]:
    """Parse text like ``3302:20E9`` into two integers."""
    m = re.fullmatch(r"\s*(?:0x)?([0-9a-fA-F]{4})\s*[:\s,]\s*(?:0x)?([0-9a-fA-F]{4})\s*", text)
    if not m:
        raise ValueError("Use the form 3302:20E9")
    return int(m.group(1), 16), int(m.group(2), 16)


def open_hint() -> str:
    """A platform-specific tip for when the OS refuses access to the device."""
    s = platform.system()
    if s == "Darwin":
        return ("macOS may be blocking access. Open System Settings > Privacy & Security > Input Monitoring, "
                "allow this app (or Terminal), then restart it.")
    if s == "Linux":
        return "Linux needs permission to use the device. See the udev rule in the README."
    return "Another program (such as the web editor) may be using it. Close it and try again."


log = logging.getLogger("arcpeq")
SAVE = 0x53


class Session:
    """One open conversation with the Arc."""

    def __init__(self, dev):
        self.dev = dev

    def _drain(self) -> None:
        """Best effort: discard stale replies. Errors here are ignored; the real read below will report them."""
        for _ in range(16):
            try:
                with HID_LOCK:
                    if not self.dev.read(64, 1):          # NOT 0: in hidapi a timeout of 0 means "block forever"
                        return
            except (OSError, ValueError):
                return

    def xfer(self, cmd: int, rw: int, payload: bytes = b"\0\0\0\0", timeout: float = 0.8) -> bytes:
        """Send one command and return the 4-byte reply. Drains stale replies first and retries once on a USB hiccup.
        """
        last_err = None
        for attempt in range(2):                         # one retry for a transient USB hiccup
            self._drain()
            try:
                with HID_LOCK:
                    if self.dev.write(packet(cmd, rw, payload)) == -1:
                        raise OSError("write failed")
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    with HID_LOCK:
                        r = self.dev.read(64, 50)
                    if r:
                        parsed = parse_reply(r)
                        if parsed and parsed[0] == cmd and parsed[1] == rw:
                            return parsed[2]
                last_err = "no answer"
            except (OSError, ValueError) as e:
                last_err = str(e) or "USB error"
                log.warning("xfer cmd=0x%02x rw=0x%02x attempt %d: %s", cmd, rw, attempt + 1, last_err)
                time.sleep(0.05)
        if last_err == "no answer":
            raise DeviceError("The device didn't answer. Try unplugging and reconnecting it.")
        raise DeviceError(f"USB communication error ({last_err}). Unplug the device, plug it back in and press ↻.")

    def _write(self, cmd: int, payload: bytes) -> None:
        """Write one register and require the device's acknowledgement."""
        if self.xfer(cmd, WRITE, payload)[0] != 3:
            raise DeviceError(f"The device rejected command 0x{cmd:02x}; nothing further was sent.")

    def version(self) -> str:
        """Read the firmware version string."""
        raw = self.xfer(4, READ)[:4] + self.xfer(5, READ)[:1]
        return clean_text(re.sub(rb"[^0-9A-Za-z._-]", b"", raw.replace(b"\0", b"")).decode("ascii", "ignore"), 12)

    def read_bands(self) -> list[Band]:
        """Read all five bands from the device."""
        out = []
        for i in range(NUM_BANDS):
            try:
                out.append(decode_band(self.xfer(0x26 + 2 * i, READ), self.xfer(0x27 + 2 * i, READ)))
            except ValueError as e:
                raise Unsupported(f"The device holds settings this app can't show ({e}). Nothing was changed.")
        return out

    def write_bands(self, bands) -> None:
        """Write all five bands live (validated first; refuses boosts above the hard cap)."""
        bands = validate_bands(bands)
        if peak_boost(bands) > MAX_BOOST_DB:
            raise DeviceError(f"This sound boosts by more than +{MAX_BOOST_DB:.0f} dB in total, which is too much for "
                              "in-ear monitors. Nothing was sent. Lower some of the gains.")
        plan = [item for i, b in enumerate(bands) for item in encode_band(i, b)]   # encode everything first
        self._write(0x02, bytes([0x65, 0, 0, 0]))
        for cmd, payload in plan:
            self._write(cmd, payload)
        self._write(0x24, bytes([1, 0, 0, 0]))
        self._write(0x02, bytes([0x65, 0, 0, 0]))

    def save(self) -> None:
        """Commit the PEQ to the device's memory so it survives unplugging. Mirrors the vendor editor: rw 0x53 ('S')
        for cmd 0x26 carrying Band 1's current bytes. Verified on firmware 1.0.1 (settings survive a replug)."""
        cur = self.xfer(0x26, READ)
        if self.xfer(0x26, SAVE, cur)[0] != 3:
            raise DeviceError("The device didn't confirm saving.")

    def set_volume(self, step: int) -> None:
        """Set the DAC output level (steps 9..15 = -6..+3 dB). Applies instantly; can't be read back."""
        if not (VOL_MIN <= step <= VOL_MAX):
            raise ValueError(f"volume step {step} out of range")
        self._write(0x3B, bytes([step, 0, 0, 0]))

    def close(self):
        """Release the HID handle."""
        try:
            with HID_LOCK:
                self.dev.close()
        except Exception:
            pass


class RealBackend:
    """Talks to real USB HID devices through hidapi."""
    def list_devices(self) -> list[DeviceInfo]:
        """Enumerate HID devices, group their interfaces per physical device and mark verified ones."""
        import hid
        with HID_LOCK:
            infos = hid.enumerate()
        extra = set(_user_devices())
        profiles = {(p.vid, p.pid): p for p in PROFILES}
        groups: dict[str, dict] = {}
        for d in infos:
            vid, pid = d["vendor_id"], d["product_id"]
            prof = profiles.get((vid, pid))
            if not prof and vid not in KNOWN_MAKER_VIDS and (vid, pid) not in extra:
                continue
            serial = clean_text(d.get("serial_number") or "", 24)
            key = f"{vid:04x}:{pid:04x}:{serial or 'x'}"
            g = groups.setdefault(key, dict(
                name=prof.name if prof else clean_text(d.get("product_string") or f"USB device {vid:04X}:{pid:04X}"),
                vid=vid, pid=pid, serial=serial, verified=bool(prof),
                firmwares=prof.firmwares if prof else (), paths=[]))
            g["paths"].append(d["path"])
            g.setdefault("ifaces", []).append(d.get("interface_number", -1))
        out = []
        for key, g in groups.items():
            # no serial and the same interface listed twice = two identical units we cannot tell apart
            amb = (not g["serial"]) and len(set(g["ifaces"])) < len(g["ifaces"])
            out.append(DeviceInfo(key, g["name"], g["vid"], g["pid"], g["serial"], g["verified"],
                                  g["firmwares"], tuple(g["paths"]), amb))
        # same model twice with no serial: number them so the user can tell them apart
        seen: dict[str, int] = {}
        numbered = []
        for d in out:
            n = seen[d.name] = seen.get(d.name, 0) + 1
            numbered.append(d if n == 1 else DeviceInfo(d.key + f"#{n}", f"{d.name} #{n}", d.vid, d.pid, d.serial,
                                                        d.verified, d.firmwares, d.paths))
        return sorted(numbered, key=lambda d: (not d.verified, d.name, d.key))

    def open(self, info: DeviceInfo, expect_fw: str | None = None) -> Session:
        """Open the device (trying each of its HID interfaces) and check it is still the selected one."""
        import hid
        if info.ambiguous:
            raise DeviceError("Two identical devices without serial numbers are connected, so this app can't tell "
                              "which is which. Unplug one and press rescan.")
        last = None
        for path in info.paths:                  # the device may expose several interfaces; keep the one that answers
            dev = hid.device()
            try:
                with HID_LOCK:
                    dev.open_path(path)
                    dev.set_nonblocking(False)
                s = Session(dev)
                fw = s.version()
                if expect_fw and fw != expect_fw:
                    s.close()
                    raise DeviceChanged("The device changed since you selected it. Select it again.")
                return s
            except DeviceChanged:
                raise
            except DeviceError as e:
                last = e
            except OSError:
                last = DeviceError(f"Couldn't open the device. {open_hint()}")
            try:
                with HID_LOCK:
                    dev.close()
            except Exception:
                pass
        raise last or DeviceError(f"Couldn't talk to the device. {open_hint()}")


class _FakeDev:
    """In-memory stand-in so the UI can be tried without hardware."""

    def __init__(self, store):
        self.store, self.reply = store, None

    def write(self, data):
        """Pretend to send a report (models the Arc's registers)."""
        cmd, rw, payload = data[1], data[5], bytes(data[7:11])
        time.sleep(0.01)
        head = bytes([0x4B, cmd, 0, 0, 0, rw, 0])
        if rw == READ:
            if cmd == 4:
                resp = b"1.0."
            elif cmd == 5:
                resp = b"1\0\0\0"
            else:
                i, which = divmod(cmd - 0x26, 2)
                resp = encode_band(i, self.store["bands"][i])[which][1]
        else:
            if rw == SAVE and not self.store.get("no_restart"):   # the real Arc restarts its USB connection after saving
                start = time.time() + self.store.get("restart_delay", 0.0)
                self.store["gone_from"], self.store["gone_until"] = start, start + 1.2
                self.store["level_reset_due"] = True
            if cmd == 0x3B and rw == WRITE:
                self.store["vol"] = payload[0]
            if 0x26 <= cmd <= 0x2F:
                i, which = divmod(cmd - 0x26, 2)
                cur = dict(encode_band(i, self.store["bands"][i]))
                cur[cmd] = payload
                self.store["bands"][i] = decode_band(cur[0x26 + 2 * i], cur[0x27 + 2 * i])
            resp = b"\x03\0\0\0"
        self.reply = head + resp

    def read(self, n, timeout=0):
        """Pretend to read a report (asserts callers never use a blocking timeout of 0)."""
        if not timeout:                       # real hidapi blocks forever on timeout 0; fail loudly instead
            raise AssertionError("read() with timeout 0 would block on real hardware")
        r, self.reply = self.reply, None
        return list(r) if r else []

    def close(self):
        """No-op."""
        pass


class DemoBackend:
    """Pretends two devices exist so the picker can be tried."""

    def __init__(self):
        self.t0 = time.time()
        self.stores = {"a": {"bands": list(LIBRARY[0].bands)}, "b": {"bands": list(FLAT.bands)}}
        self.devices = [
            DeviceInfo("3302:20e9:demoA", "Audiocular Arc", 0x3302, 0x20E9, "DEMO-A", True, ("1.0.1",), ("a",)),
            DeviceInfo("3302:2001:demoB", "Audiocular Tune", 0x3302, 0x2001, "DEMO-B", False, (), ("b",)),
        ]

    def _gone(self, info):
        """True while the pretend device is 'restarting'."""
        st = self.stores[info.paths[0]]
        now = time.time()
        if st.get("level_reset_due") and now >= st.get("gone_until", 0):   # after its restart the level is back to default
            st["vol"], st["level_reset_due"] = 13, False
        return st.get("gone_from", 0) <= now < st.get("gone_until", 0)

    def list_devices(self):
        """Two pretend devices: a verified one and an unverified one."""
        if time.time() - self.t0 <= 1.0:
            return []
        return [d for d in self.devices if not self._gone(d)]

    def open(self, info, expect_fw=None):
        """Open a pretend device (used by ``--demo`` and the tests)."""
        if self._gone(info):
            raise DeviceError("Couldn't open the device.")
        s = Session(_FakeDev(self.stores[info.paths[0]]))
        if expect_fw and s.version() != expect_fw:
            raise DeviceError("The device changed since you selected it. Select it again.")
        return s
