import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from arcpeq import core
from arcpeq.core import *
from arcpeq.device import DemoBackend, DeviceError, Session, parse_vidpid, _FakeDev

CAPTURE = {0x26: "2d 00 5a 00", 0x27: "bc 02 0b 00", 0x28: "ec ff fa 00", 0x29: "e8 03 08 00", 0x2a: "19 00 c4 09",
           0x2b: "b0 04 08 00", 0x2c: "ec ff 70 17", 0x2d: "d0 07 08 00", 0x2e: "0f 00 10 27", 0x2f: "bc 02 0c 00"}


class Protocol(unittest.TestCase):
    def test_decode_real_capture_and_roundtrip(self):
        g = lambda c: bytes.fromhex(CAPTURE[c])
        bands = [decode_band(g(0x26 + 2 * i), g(0x27 + 2 * i)) for i in range(5)]
        self.assertEqual(bands[0], Band(LOW_SHELF, 90, 0.7, 4.5))
        self.assertEqual(bands[4], Band(HIGH_SHELF, 10000, 0.7, 1.5))
        for i, b in enumerate(bands):
            for cmd, p in encode_band(i, b):
                self.assertEqual(p, g(cmd))

    def test_encode_rejects_bad_values(self):
        for bad in (Band(99, 100, 1, 0), Band(PEAK, 0, 1, 0), Band(PEAK, 100, 0, 0), Band(PEAK, 100, 70, 0),
                    Band(PEAK, 100, 1, 1e9), Band(PEAK, 100, 1, float("nan")), Band(PEAK, 100.5, 1, 0)):
            with self.assertRaises(ValueError):
                encode_band(0, bad)

    def test_decode_rejects_garbage(self):
        with self.assertRaises(ValueError):
            decode_band(bytes.fromhex("ff 7f 00 00"), bytes.fromhex("00 00 63 00"))

    def test_bad_band_means_nothing_is_sent(self):
        sent = []
        class Dev:
            def write(self, d): sent.append(d)
            def read(self, n, t=0): return []
            def close(self): pass
        bands = list(LIBRARY[0].bands); bands[3] = Band(PEAK, 0, 1, 0)
        with self.assertRaises(ValueError):
            Session(Dev()).write_bands(bands)
        self.assertEqual(sent, [])

    def test_fake_device_roundtrip(self):
        s = DemoBackend().open(DemoBackend().devices[0])
        s.write_bands(LIBRARY[1].bands)
        self.assertEqual(s.read_bands(), LIBRARY[1].bands)
        self.assertEqual(s.version(), "1.0.1")


class Captured(unittest.TestCase):
    """Check our output byte-for-byte against what the official editor sent (band 5 switched off, volume +3)."""
    def test_apply_sequence_matches_official_editor(self):
        sent = []
        class Dev:
            def __init__(s): s.q = None
            def write(s, d): sent.append(bytes(d)); s.q = bytes([0x4B, d[1], 0, 0, 0, d[5], 0, 3, 0, 0, 0])
            def read(s, n, t=0): r, s.q = s.q, None; return list(r) if r else []
            def close(s): pass
        bands = [decode_band(bytes.fromhex(CAPTURE[0x26 + 2 * i]), bytes.fromhex(CAPTURE[0x27 + 2 * i])) for i in range(5)]
        bands[0] = bands[0].with_(gain=5.0)            # editor had Band 1 at +5.0
        bands[4] = bands[4].with_(gain=0.0)            # Band 5 switched off
        s = Session(Dev()); s.set_volume(15); s.write_bands(bands)
        official = """4b 3b 00 00 00 57 00 0f 00 00 00
4b 02 00 00 00 57 00 65 00 00 00
4b 26 00 00 00 57 00 32 00 5a 00
4b 27 00 00 00 57 00 bc 02 0b 00
4b 28 00 00 00 57 00 ec ff fa 00
4b 29 00 00 00 57 00 e8 03 08 00
4b 2a 00 00 00 57 00 19 00 c4 09
4b 2b 00 00 00 57 00 b0 04 08 00
4b 2c 00 00 00 57 00 ec ff 70 17
4b 2d 00 00 00 57 00 d0 07 08 00
4b 2e 00 00 00 57 00 00 00 10 27
4b 2f 00 00 00 57 00 bc 02 0c 00
4b 24 00 00 00 57 00 01 00 00 00
4b 02 00 00 00 57 00 65 00 00 00""".split("\n")
        self.assertEqual([p.hex(" ") for p in sent], official)

    def test_volume_mapping(self):
        self.assertEqual([volume_db(s) for s in range(9, 16)], [-6, -4.5, -3, -1.5, 0, 1.5, 3])
        self.assertEqual(volume_step(-3), 11); self.assertEqual(volume_step(99), 15); self.assertEqual(volume_step(-99), 9)
        self.assertEqual(suggest_volume_db(5.0), -4.5); self.assertEqual(suggest_volume_db(0.5), 0.0)
        self.assertGreaterEqual(suggest_volume_db(40), -6.0)

    def test_volume_out_of_range_sends_nothing(self):
        sent = []
        class Dev:
            def write(s, d): sent.append(d)
            def read(s, n, t=0): return []
            def close(s): pass
        for bad in (8, 16, 0, 255, -1):
            with self.assertRaises(ValueError): Session(Dev()).set_volume(bad)
        self.assertEqual(sent, [])


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = mock.patch.object(Path, "home", return_value=Path(self.tmp)); self.p.start()
    def tearDown(self): self.p.stop()

    def test_presets_roundtrip_and_permissions(self):
        pr = [Preset("a", "Mine", "", list(LIBRARY[0].bands), False)]
        save_user_presets(pr)
        out, warn = load_user_presets()
        self.assertEqual([x.name for x in out], ["Mine"]); self.assertEqual(warn, [])
        self.assertEqual(oct(os.stat(core._store()).st_mode & 0o777), "0o600")

    def test_one_bad_preset_does_not_wipe_the_rest(self):
        good = {"id": "g", "name": "Good", "bands": [b.__dict__ for b in LIBRARY[0].bands]}
        bad_band = dict(good, id="b", name="Bad"); bad_band["bands"] = [dict(b.__dict__, extra=1) for b in LIBRARY[0].bands]
        zero = dict(good, id="z", name="Zero"); zero["bands"] = [dict(b.__dict__) for b in LIBRARY[0].bands]; zero["bands"][0]["freq"] = 0
        core._store().write_text(json.dumps([good, bad_band, zero, {"nope": 1}]))
        out, warn = load_user_presets()
        self.assertEqual([x.name for x in out], ["Good"]); self.assertTrue(warn)
        self.assertTrue(list(core._store().parent.glob("presets.json.*.bad")))

    def test_corrupt_and_hostile_files(self):
        for text in ("not json", "[" * 100000, "{}", "null"):
            core._store().write_text(text)
            out, warn = load_user_presets()
            self.assertEqual(out, []); self.assertTrue(warn)

    def test_names_are_cleaned(self):
        core._store().write_text(json.dumps([{"id": "x", "name": "Hi\nFilter 9: ON PK", "bands": [b.__dict__ for b in LIBRARY[0].bands]}]))
        out, _ = load_user_presets()
        self.assertNotIn("\n", out[0].name)
        self.assertNotIn("\n", to_apo(LIBRARY[0].bands, "a\nFilter 1: ON PK Fc 100 Hz Gain 12 dB Q 1").split("\n")[0])


class Import(unittest.TestCase):
    def test_basic_and_round_trip(self):
        bands, notes = parse_apo(to_apo(LIBRARY[2].bands))
        self.assertEqual(bands, LIBRARY[2].bands)

    def test_extra_filters_off_filters_and_preamp(self):
        txt = "Preamp: -4 dB\nFilter 1: OFF PK Fc 100 Hz Gain 3 dB Q 1\n" + "\n".join(
            f"Filter {i}: ON PK Fc {200*i} Hz Gain 1.0 dB Q 1.0" for i in range(1, 8))
        bands, notes = parse_apo(txt)
        self.assertEqual(len(bands), 5); self.assertTrue(any("first 5" in n for n in notes))

    def test_no_filters_is_an_error(self):
        for t in ("", "hello", "Filter 1: ON PK Fc 1.2.3 Hz Gain 1 dB", "Filter 1: ON PK Fc " + "9" * 400 + " Hz Gain 1 dB"):
            with self.assertRaises(ValueError): parse_apo(t)

    def test_utf16_and_speed(self):
        txt = "Filter 1: ON PK Fc 100 Hz Gain 3 dB Q 1\n"
        self.assertEqual(parse_apo(decode_text(txt.encode("utf-16")))[0][0].gain, 3.0)
        import time; t = time.time()
        with self.assertRaises(ValueError): parse_apo("Filter" + " " * 400000 + ":")
        parse_apo(txt * 100000)
        self.assertLess(time.time() - t, 2)

    def test_values_are_clamped(self):
        b = parse_apo("Filter 1: ON PK Fc 99999 Hz Gain 50 dB Q 500")[0][0]
        self.assertEqual((b.freq, b.gain, b.q), (20000, 12.0, 10.0))


class Misc(unittest.TestCase):
    def test_vidpid(self):
        self.assertEqual(parse_vidpid("3302:20e9"), (0x3302, 0x20E9))
        for bad in ("", "zz:12", "3302", "3302:20e9;rm"):
            with self.assertRaises(ValueError): parse_vidpid(bad)


class SaveCommand(unittest.TestCase):
    def test_save_sends_S_for_band1_and_needs_ack(self):
        sent = []
        store = {"bands": list(FLAT.bands)}
        dev = _FakeDev(store)
        ow = dev.write
        dev.write = lambda d: (sent.append(bytes(d)), ow(d))[1]
        Session(dev).save()
        s_pkts = [p for p in sent if p[5] == 0x53]
        self.assertEqual(len(s_pkts), 1)
        self.assertEqual(s_pkts[0][1], 0x26)
        self.assertEqual(store["bands"][0], FLAT.bands[0])      # re-sent the band's own bytes, no change


class FlakyUSB(unittest.TestCase):
    def _dev(self, fail_reads):
        store = {"bands": list(FLAT.bands)}
        dev = _FakeDev(store)
        real, n = dev.read, {"fails": fail_reads}

        def read(size, timeout=0):
            if timeout == 50 and n["fails"] > 0:
                n["fails"] -= 1
                raise OSError("read error")
            return real(size, timeout)
        dev.read = read
        return dev

    def test_one_hiccup_is_retried(self):
        self.assertEqual(len(Session(self._dev(1)).read_bands()), 5)

    def test_persistent_error_becomes_friendly_device_error(self):
        with self.assertRaises(DeviceError) as cm:
            Session(self._dev(99)).read_bands()
        self.assertIn("USB communication error", str(cm.exception))


class Backups(unittest.TestCase):
    def test_list_backups_newest_first_skips_bad_files(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import arcpeq.core as core
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(core, "data_dir", return_value=tmp):
            core.save_backup("Arc", FLAT.bands)
            (tmp / "backups" / "99999999-000000-bad.json").write_text("{not json")
            (tmp / "backups" / "20000101-000000-old.json").write_text(
                '{"device":"x","bands":[{"type":8,"freq":1,"q":1,"gain":99}]}')   # invalid values
            found = core.list_backups()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][2], "Arc")
        self.assertEqual(found[0][3], list(FLAT.bands))


class Hardening(unittest.TestCase):
    def test_float_type_rejected(self):
        with self.assertRaises(ValueError):
            validate_band(Band(8.0, 1000, 1.0, 0.0))

    def test_loaded_values_are_normalised(self):
        b = core._band_from_json({"type": 8, "freq": 1000, "q": 1.23456, "gain": 2.34})
        self.assertEqual((b.q, b.gain), (1.235, 2.3))

    def test_backups_unique_and_first_pinned(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(core, "data_dir", return_value=tmp):
            with mock.patch.object(core.time, "strftime", return_value="20260101-000000"):
                for _ in range(30):
                    core.save_backup("Arc", FLAT.bands)
            files = list((tmp / "backups").glob("*.json"))
            self.assertEqual(len([f for f in files if "-first-" in f.name]), 1)
            self.assertEqual(len(files), 21)
            self.assertEqual(len(core.list_backups()), 21)

    def test_corrupt_presets_kept_with_timestamp_and_oserror_untouched(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(core, "data_dir", return_value=tmp):
            (tmp / "presets.json").write_text("{nope")
            out, warn = core.load_user_presets()
            self.assertEqual(out, [])
            self.assertTrue(warn)
            self.assertFalse((tmp / "presets.json").exists())
            self.assertTrue(list(tmp.glob("presets.json.*.bad")))

    def test_write_bands_refuses_over_cap(self):
        from arcpeq.device import RealBackend
        big = [Band(PEAK, 1000 + i, 1.0, 12.0) for i in range(5)]
        self.assertGreater(peak_boost(big), MAX_BOOST_DB)
        with self.assertRaises(DeviceError):
            Session(_FakeDev({})).write_bands(big)


class Library(unittest.TestCase):
    def test_every_builtin_preset_is_valid_and_not_extreme(self):
        ids = [p.id for p in LIBRARY]
        self.assertEqual(len(ids), len(set(ids)))
        for p in LIBRARY:
            self.assertEqual(len(p.bands), 5, p.id)
            validate_bands(p.bands)
            self.assertLessEqual(peak_boost(p.bands), 9.0, f"{p.name} boosts too much")
        self.assertIn("gaming", ids)
        self.assertIn("gaming_story", ids)

    def test_competitive_gaming_cuts_bass_and_lifts_footstep_range(self):
        g = next(p for p in LIBRARY if p.id == "gaming")
        self.assertLess(g.bands[0].gain, 0)
        self.assertTrue(any(2000 <= b.freq <= 5000 and b.gain >= 2.5 for b in g.bands))


if __name__ == "__main__":
    unittest.main()
