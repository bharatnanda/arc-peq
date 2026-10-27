"""Protocol, filter maths and presets for the Audiocular Arc's 5-band PEQ.

Wire format (decoded from the official editor), HID report ID 0x4B, 10 data bytes:
    [cmd, 0, 0, 0, rw, 0, p0, p1, p2, p3]        rw = 0x52 'R' read | 0x57 'W' write
Band i uses cmd 0x26+2i -> gain (int16, 0.1 dB) + freq (uint16 Hz)
              cmd 0x27+2i -> Q*1000 (uint16)      + filter type (uint16)
Filter types: 8 = peak, 11 = low shelf, 12 = high shelf.   A write is acked with 03 00 00 00.
"""
from __future__ import annotations

import json
import math
import struct
from functools import lru_cache
from dataclasses import dataclass, replace
from pathlib import Path

REPORT_ID = 0x4B
READ, WRITE = 0x52, 0x57
PEAK, LOW_SHELF, HIGH_SHELF = 8, 11, 12
TYPE_NAMES = {PEAK: "Peak", LOW_SHELF: "Low Shelf", HIGH_SHELF: "High Shelf"}
NUM_BANDS = 5
GAIN_LIMIT = 12.0
FREQ_MIN, FREQ_MAX = 20, 20000


Q_MIN, Q_MAX = 0.1, 10.0

# DAC volume (command 0x3b): one unit = 1.5 dB, 0x0d = 0 dB, range 0x09 (-6 dB) .. 0x0f (+3 dB)
VOL_ZERO, VOL_MIN, VOL_MAX, VOL_STEP_DB = 13, 9, 15, 1.5


def volume_db(step: int) -> float:
    """Convert an output-level step (9..15) to dB (13 = 0 dB, 1.5 dB per step)."""
    return (step - VOL_ZERO) * VOL_STEP_DB


def volume_step(db: float) -> int:
    """Convert dB to the nearest valid output-level step, clamped to the supported range."""
    return min(max(round(db / VOL_STEP_DB) + VOL_ZERO, VOL_MIN), VOL_MAX)


def suggest_volume_db(boost_db: float) -> float:
    """Output level that leaves headroom for a boost of the given size (never above 0 dB)."""
    if boost_db <= 1.5:
        return 0.0
    return -min(6.0, round(0.9 * boost_db / VOL_STEP_DB) * VOL_STEP_DB)



@dataclass(frozen=True)
class Band:
    """One filter: ``type`` (peak / low shelf / high shelf), ``freq`` in Hz, ``q`` (width) and ``gain`` in dB."""
    type: int
    freq: int
    q: float
    gain: float

    def with_(self, **kw) -> "Band":
        """Return a copy with some fields replaced (bands are immutable)."""
        return replace(self, **kw)


def validate_band(b) -> "Band":
    """Raise ValueError unless every field is a finite number inside the range the Arc supports."""
    if not isinstance(b, Band):
        raise ValueError("not a band")
    for name in ("freq", "q", "gain"):
        v = getattr(b, name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{name} is not a finite number")
    if type(b.type) is not int or b.type not in TYPE_NAMES:
        raise ValueError(f"unknown filter type {b.type!r}")
    if not (FREQ_MIN <= b.freq <= FREQ_MAX) or int(b.freq) != b.freq:
        raise ValueError(f"frequency {b.freq} outside {FREQ_MIN}-{FREQ_MAX} Hz")
    if not (Q_MIN <= b.q <= Q_MAX):
        raise ValueError(f"Q {b.q} outside {Q_MIN}-{Q_MAX}")
    if not (-GAIN_LIMIT <= b.gain <= GAIN_LIMIT):
        raise ValueError(f"gain {b.gain} outside +/-{GAIN_LIMIT} dB")
    return b


def validate_bands(bands) -> list:
    """Validate every band and the band count; returns the list or raises ValueError."""
    bands = list(bands)
    if len(bands) != NUM_BANDS:
        raise ValueError(f"expected {NUM_BANDS} bands, got {len(bands)}")
    return [validate_band(b) for b in bands]


def clean_text(s, limit=60) -> str:
    """Single-line, printable, length-limited text (names that come from files or devices)."""
    s = "".join(ch for ch in str(s) if ch.isprintable())
    return s.strip()[:limit]


# ---------------------------------------------------------------- protocol

def packet(cmd: int, rw: int, payload: bytes = b"\0\0\0\0") -> bytes:
    """Build one 11-byte HID report for command ``cmd`` (``rw`` = read/write/save)."""
    return bytes([REPORT_ID, cmd, 0, 0, 0, rw, 0]) + bytes(payload)[:4]


def parse_reply(raw) -> tuple[int, int, bytes] | None:
    """Split a reply into (command, rw, 4 payload bytes), or None if it isn't a valid reply."""
    b = bytes(raw)
    if len(b) >= 10 and b[0] != REPORT_ID:      # some stacks strip the report id
        b = bytes([REPORT_ID]) + b
    if len(b) < 11 or b[0] != REPORT_ID:
        return None
    return b[1], b[5], b[7:11]


def encode_band(i: int, band: Band) -> list[tuple[int, bytes]]:
    """Turn band ``i`` into its two (command, payload) writes: gain+freq, then Q+type."""
    validate_band(band)
    a = struct.pack("<hH", round(band.gain * 10), int(band.freq))
    b = struct.pack("<HH", round(band.q * 1000), band.type)
    return [(0x26 + 2 * i, a), (0x27 + 2 * i, b)]


def decode_band(a: bytes, b: bytes) -> Band:
    """Inverse of ``encode_band``: rebuild a Band from the two 4-byte replies."""
    gain, freq = struct.unpack("<hH", a)
    q, typ = struct.unpack("<HH", b)
    return validate_band(Band(typ, freq, q / 1000, gain / 10))


# ---------------------------------------------------------------- maths

SAMPLE_RATE = 48000.0


def band_db(band: Band, f: float) -> float:
    """Magnitude response (dB) of one biquad band at frequency f (RBJ cookbook)."""
    if band.gain == 0:
        return 0.0
    A = 10 ** (band.gain / 40)
    w0 = 2 * math.pi * band.freq / SAMPLE_RATE
    alpha = math.sin(w0) / (2 * max(band.q, 0.1))
    c = math.cos(w0)
    if band.type == PEAK:
        b0, b1, b2 = 1 + alpha * A, -2 * c, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * c, 1 - alpha / A
    else:
        s = 2 * math.sqrt(A) * alpha
        if band.type == LOW_SHELF:
            b0 = A * ((A + 1) - (A - 1) * c + s)
            b1 = 2 * A * ((A - 1) - (A + 1) * c)
            b2 = A * ((A + 1) - (A - 1) * c - s)
            a0 = (A + 1) + (A - 1) * c + s
            a1 = -2 * ((A - 1) + (A + 1) * c)
            a2 = (A + 1) + (A - 1) * c - s
        else:
            b0 = A * ((A + 1) + (A - 1) * c + s)
            b1 = -2 * A * ((A - 1) + (A + 1) * c)
            b2 = A * ((A + 1) + (A - 1) * c - s)
            a0 = (A + 1) - (A - 1) * c + s
            a1 = 2 * ((A - 1) - (A + 1) * c)
            a2 = (A + 1) - (A - 1) * c - s
    w = 2 * math.pi * f / SAMPLE_RATE
    cw, sw, c2, s2 = math.cos(w), math.sin(w), math.cos(2 * w), math.sin(2 * w)
    nr, ni = b0 + b1 * cw + b2 * c2, -(b1 * sw + b2 * s2)
    dr, di = a0 + a1 * cw + a2 * c2, -(a1 * sw + a2 * s2)
    return 10 * math.log10(max((nr * nr + ni * ni) / (dr * dr + di * di), 1e-12))


def total_db(bands, f: float) -> float:
    """Combined response of all bands at frequency ``f`` (bands add in dB)."""
    return sum(band_db(b, f) for b in bands)


@lru_cache(maxsize=256)
def _peak_boost(bands: tuple) -> float:
    """Cached scan for the largest total boost; narrow peaks are checked exactly at their centre."""
    freqs = [20.0 * 1.01 ** k for k in range(int(math.log(1000) / math.log(1.01)) + 2)]
    freqs += [float(b.freq) for b in bands]                       # narrow peaks sit exactly on a band centre
    return max(0.0, max(total_db(bands, min(f, 20000.0)) for f in freqs))


MAX_BOOST_DB = 15.0      # hard ceiling on the combined boost: far beyond this is not useful through in-ear monitors


def peak_boost(bands) -> float:
    """Largest total boost (dB) over the audio range."""
    return _peak_boost(tuple(bands))


def snap_freq(f: float) -> int:
    """Round a frequency to a friendly step (1 Hz below 100, 5 up to 1 k, 50 up to 10 k, 100 above)."""
    f = min(max(f, FREQ_MIN), FREQ_MAX)
    step = 1 if f < 100 else 5 if f < 1000 else 50 if f < 10000 else 100
    return int(min(max(round(f / step) * step, FREQ_MIN), FREQ_MAX))


# ---------------------------------------------------------------- presets

@dataclass
class Preset:
    """A named set of five bands. ``builtin`` presets can't be edited or deleted."""
    id: str
    name: str
    detail: str
    bands: list
    builtin: bool = True


def _p(id_, name, detail, rows) -> Preset:
    """Shorthand to build a Preset from (type, freq, q, gain) rows."""
    return Preset(id_, name, detail, [Band(t, f, q, g) for t, f, q, g in rows])


FLAT = _p("flat", "Flat", "No colouring. A clean starting point.", [
    (LOW_SHELF, 90, 0.7, 0), (PEAK, 250, 1.0, 0), (PEAK, 2500, 1.0, 0),
    (PEAK, 6000, 1.0, 0), (HIGH_SHELF, 10000, 0.7, 0)])

# Band 1 = bass shelf, 2 = mud control, 3 = vocal / presence, 4 = harshness control, 5 = air shelf
LIBRARY = [
    _p("balanced", "Balanced Daily", "Everyday music and movies. Harman-style: warm bass, clear vocals.", [
        (LOW_SHELF, 90, 0.7, 4.5), (PEAK, 250, 1.0, -2.0), (PEAK, 2500, 1.2, 2.5),
        (PEAK, 6000, 2.0, -2.0), (HIGH_SHELF, 10000, 0.7, 1.5)]),
    _p("bass", "Bass Boost", "Strong, punchy bass for pop, EDM and action movies.", [
        (LOW_SHELF, 100, 0.7, 7.5), (PEAK, 250, 1.0, -2.5), (PEAK, 3000, 1.0, 1.5),
        (PEAK, 6000, 2.0, -1.5), (HIGH_SHELF, 10000, 0.7, 1.0)]),
    _p("vocal", "Vocal Clarity", "Podcasts, calls and dialogue. Voices up front.", [
        (LOW_SHELF, 100, 0.7, 2.0), (PEAK, 300, 1.0, -2.0), (PEAK, 2800, 1.2, 3.5),
        (PEAK, 5500, 2.0, -2.5), (HIGH_SHELF, 9000, 0.7, 1.0)]),
    _p("cinema", "Cinematic", "Movies and series. Deep rumble, open and spacious.", [
        (LOW_SHELF, 70, 0.7, 6.0), (PEAK, 220, 1.0, -2.0), (PEAK, 2000, 1.0, 2.0),
        (PEAK, 5000, 1.5, -1.5), (HIGH_SHELF, 8000, 0.7, 2.0)]),
    _p("gaming", "Gaming: Competitive", "Hear footsteps and reloads early. Less rumble, more 1–5 kHz detail.", [
        (LOW_SHELF, 150, 0.7, -2.5), (PEAK, 1200, 1.0, 2.0), (PEAK, 2800, 1.2, 3.5),
        (PEAK, 4500, 1.5, 2.5), (HIGH_SHELF, 8000, 0.7, 1.0)]),
    _p("gaming_story", "Gaming: Immersive", "Story and open-world games. Weighty explosions, open space.", [
        (LOW_SHELF, 90, 0.7, 4.0), (PEAK, 300, 1.0, -1.5), (PEAK, 3000, 1.2, 2.0),
        (PEAK, 6000, 2.0, -1.5), (HIGH_SHELF, 10000, 0.7, 1.5)]),
    _p("edm", "EDM & Hip-Hop", "Club and hip-hop. Heavy sub-bass, crisp hats.", [
        (LOW_SHELF, 80, 0.7, 8.0), (PEAK, 200, 1.0, -3.0), (PEAK, 2500, 1.0, 1.5),
        (PEAK, 7000, 2.0, -2.0), (HIGH_SHELF, 10000, 0.7, 2.0)]),
    _p("rock", "Rock & Metal", "Guitars with bite, solid kick, smooth cymbals.", [
        (LOW_SHELF, 100, 0.7, 3.5), (PEAK, 400, 1.0, -2.5), (PEAK, 3000, 1.0, 2.0),
        (PEAK, 5500, 2.0, -2.0), (HIGH_SHELF, 9000, 0.7, 1.5)]),
    _p("bollywood", "Bollywood & Pop", "Warm vocals with a lively low end.", [
        (LOW_SHELF, 90, 0.7, 5.0), (PEAK, 250, 1.0, -1.5), (PEAK, 2200, 1.0, 3.0),
        (PEAK, 7000, 2.0, -2.5), (HIGH_SHELF, 11000, 0.7, 1.0)]),
    _p("acoustic", "Acoustic & Classical", "Acoustic, jazz and classical. Natural and airy.", [
        (LOW_SHELF, 100, 0.7, 1.5), (PEAK, 300, 1.0, -1.0), (PEAK, 3000, 1.0, 1.5),
        (PEAK, 6000, 2.0, -1.5), (HIGH_SHELF, 10000, 0.7, 2.0)]),
    _p("night", "Late Night", "Quiet listening. Keeps bass and detail alive.", [
        (LOW_SHELF, 90, 0.7, 5.5), (PEAK, 300, 1.0, -2.0), (PEAK, 3000, 1.0, 2.0),
        (PEAK, 6000, 2.0, -2.0), (HIGH_SHELF, 9000, 0.7, 2.5)]),
    _p("smooth", "Smooth & Easy", "Relaxed, less tiring tuning for long sessions.", [
        (LOW_SHELF, 100, 0.7, 3.0), (PEAK, 250, 1.0, -0.5), (PEAK, 3000, 1.0, -1.0),
        (PEAK, 6000, 2.0, -1.0), (HIGH_SHELF, 10000, 0.7, 1.0)]),
]

# ---------------------------------------------------------------- user presets

import os
import stat
import tempfile
import time

MAX_FILE_BYTES = 1_000_000


def data_dir() -> Path:
    """~/.arcpeq, created private. Refuses a symlinked directory and tightens loose permissions."""
    d = Path.home() / ".arcpeq"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{d} is not a normal folder")
    if os.name == "posix":
        if st.st_uid != os.getuid():
            raise OSError(f"{d} belongs to another user")
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
    return d


def read_small_file(path, limit: int = MAX_FILE_BYTES) -> bytes:
    """Read a regular file of at most `limit` bytes. Refuses symlinks, FIFOs and devices; never blocks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("file too large")
        return data
    finally:
        os.close(fd)


def _store() -> Path:
    """Path of the user's presets file."""
    return data_dir() / "presets.json"


def atomic_write(path: Path, text: str) -> None:
    """Write a file so it is never half-written: temp file, private permissions, then rename over the target."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _band_from_json(d) -> Band:
    """Rebuild a Band from saved JSON, rejecting unknown keys and rounding to what the device can store."""
    if not isinstance(d, dict) or set(d) != {"type", "freq", "q", "gain"}:
        raise ValueError("bad band entry")
    return validate_band(Band(d["type"], d["freq"], round(float(d["q"]), 3), round(float(d["gain"]), 1)))


def load_user_presets() -> tuple[list, list]:
    """Returns (presets, warnings). Bad entries are skipped; a corrupt file is kept as presets.json.bad."""
    try:
        path = _store()
    except OSError as e:
        return [], [f"Couldn't use your settings folder ({e}). Presets can't be saved this session."]
    if not os.path.lexists(path):
        return [], []
    warnings = []
    try:
        raw = read_small_file(path)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
    except OSError as e:
        return [], [f"Your saved presets file couldn't be read right now ({e}). It was left untouched."]
    except (ValueError, RecursionError, MemoryError) as e:
        bad = path.with_name(f"presets.json.{time.strftime('%Y%m%d-%H%M%S')}.bad")
        try:
            os.replace(path, bad)
        except OSError:
            pass
        return [], [f"Your saved presets file couldn't be read ({e}). It was kept as {bad.name}."]
    out, skipped, seen = [], 0, {p.id for p in LIBRARY} | {FLAT.id}
    for d in data:
        try:
            bands = validate_bands(_band_from_json(b) for b in d["bands"])
            name, pid = clean_text(d["name"]), clean_text(d["id"], 64)
            if not name or not pid or pid in seen:
                raise ValueError("empty or duplicate")
            seen.add(pid)
            out.append(Preset(pid, name, "Custom preset", bands, False))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    if skipped:
        try:
            atomic_write(path.with_name(f"presets.json.{time.strftime('%Y%m%d-%H%M%S')}.bad"), raw.decode("utf-8", "replace"))
        except OSError:
            pass
        warnings.append(f"{skipped} saved preset(s) were invalid and skipped (original kept as a .bad copy).")
    return out, warnings


def save_user_presets(presets) -> None:
    """Write the user's presets to disk atomically."""
    atomic_write(_store(), json.dumps([{"id": x.id, "name": x.name, "bands": [b.__dict__ for b in x.bands]}
                                       for x in presets], indent=2))


def load_prefs() -> dict:
    """Read small UI preferences (e.g. whether the intro was dismissed). Never raises."""
    try:
        d = json.loads(read_small_file(data_dir() / "prefs.json", 10_000).decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def save_prefs(prefs: dict) -> None:
    """Store UI preferences. Failure is harmless and ignored."""
    try:
        atomic_write(data_dir() / "prefs.json", json.dumps(prefs))
    except OSError:
        pass


def save_backup(label: str, bands) -> Path:
    """Keep an exact copy of what the device held before we change it.
    The first backup per device is pinned (never pruned); the 20 newest others are kept."""
    d = data_dir() / "backups"
    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink():
        raise OSError("backups folder is a symlink")
    safe = "".join(ch if ch.isalnum() else "_" for ch in label)[:30] or "device"
    first = not any(d.glob(f"*-{safe}-first-*.json"))
    stamp = time.strftime('%Y%m%d-%H%M%S')
    n = 0
    while True:
        p = d / f"{stamp}-{safe}{'-first' if first else ''}-{n}.json"
        if not p.exists():
            break
        n += 1
    atomic_write(p, json.dumps({"device": clean_text(label), "bands": [b.__dict__ for b in bands]}, indent=2))
    others = sorted(x for x in d.glob("*.json") if "-first-" not in x.name)
    for old in others[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    return p


def save_raw_backup(label: str, raw_hex: str) -> None:
    """For devices we can't parse: keep the raw register bytes so nothing is lost."""
    d = data_dir() / "backups"
    d.mkdir(mode=0o700, exist_ok=True)
    atomic_write(d / f"raw-{time.strftime('%Y%m%d-%H%M%S')}.txt", clean_text(label) + "\n" + raw_hex[:4000] + "\n")


def list_backups() -> list:
    """Saved device backups, newest first: [(path, 'YYYY-MM-DD HH:MM', device name, bands)]. Bad files are skipped."""
    d = data_dir() / "backups"
    out = []
    if not d.is_dir() or d.is_symlink():
        return out
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            raw = json.loads(read_small_file(p).decode("utf-8"))
            bands = validate_bands(_band_from_json(b) for b in raw["bands"])
            stamp = p.name[:15]
            when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
            out.append((p, when, clean_text(str(raw.get("device", "device"))), bands))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


# ---------------------------------------------------------------- import / export (Equalizer APO / AutoEq text)

import re

_APO_TYPES = {"PK": PEAK, "PEQ": PEAK, "LSC": LOW_SHELF, "LS": LOW_SHELF, "LSQ": LOW_SHELF,
              "HSC": HIGH_SHELF, "HS": HIGH_SHELF, "HSQ": HIGH_SHELF}
_NUM = r"[-+]?\d{1,6}(?:\.\d{1,4})?"
_APO_RE = re.compile(rf"^Filter\s?\d{{0,3}}:\s?(ON|OFF)\s+(\w{{1,4}})\s+Fc\s+({_NUM})\s?Hz\s+Gain\s+({_NUM})\s?dB"
                     rf"(?:\s+Q\s+({_NUM}))?\s*$", re.I)


def decode_text(raw: bytes) -> str:
    """Decode an imported text file (UTF-8 or UTF-16 with BOM) into a string."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def parse_apo(text: str) -> tuple[list, list]:
    """Returns (bands, notes). Uses the first 5 usable filters and pads the rest with flat bands.
    Raises ValueError if the text contains no usable filter at all."""
    bands, notes = [], []
    for line in text.splitlines()[:2000]:
        m = _APO_RE.match(line.strip())
        if not m:
            continue
        state, kind = m.group(1).upper(), m.group(2).upper()
        if state == "OFF":
            notes.append("Skipped a filter that is switched off.")
            continue
        if kind not in _APO_TYPES:
            notes.append(f"Skipped unsupported filter type {kind[:4]}.")
            continue
        freq = int(min(max(round(float(m.group(3))), FREQ_MIN), FREQ_MAX))
        gain = round(min(max(float(m.group(4)), -GAIN_LIMIT), GAIN_LIMIT), 1)
        q = round(min(max(float(m.group(5)) if m.group(5) else 0.7, Q_MIN), Q_MAX), 3)
        bands.append(Band(_APO_TYPES[kind], freq, round(q, 3), round(gain, 1)))
    if not bands:
        raise ValueError("No EQ filters were found in that file.")
    if len(bands) > NUM_BANDS:
        notes.append(f"The file has {len(bands)} filters; the Arc has {NUM_BANDS}, so only the first {NUM_BANDS} were used.")
        bands = bands[:NUM_BANDS]
    while len(bands) < NUM_BANDS:
        bands.append(FLAT.bands[len(bands)])
    if re.search(r"^\s*Preamp\s*:", text, re.I | re.M):
        notes.append("The file's Preamp line was ignored.")
    return bands, sorted(set(notes), key=notes.index)


def to_apo(bands, name: str = "Arc PEQ") -> str:
    """Render bands as Equalizer APO text (the format AutoEq and many other tools use)."""
    kinds = {PEAK: "PK", LOW_SHELF: "LSC", HIGH_SHELF: "HSC"}
    lines = [f"# {clean_text(name)}", "Preamp: 0 dB"]
    for i, b in enumerate(bands, 1):
        lines.append(f"Filter {i}: ON {kinds[b.type]} Fc {b.freq} Hz Gain {b.gain:.1f} dB Q {b.q:.3f}".rstrip("0").rstrip("."))
    return "\n".join(lines) + "\n"
