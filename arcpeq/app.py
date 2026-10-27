"""The Arc PEQ window: everything the user sees and clicks.

Two classes do the work:

* ``Controller``  talks to the device on a single background thread, so the window never freezes while USB is slow.
  It reports back with Qt signals (``loaded``, ``applied``, ``saved``, ``failed`` ...).
* ``MainWindow``  builds the screen, keeps the *staged* state (the five bands and output level you are editing),
  and decides what is allowed (safety confirmations, boost cap, verified-device gate).

Mental model: what you see on screen is a draft. Nothing reaches the Arc until Apply (live) or Save to Arc (permanent).
Device I/O lives in ``device.py``; the filter maths, presets and file handling live in ``core.py``;
the graph and band cards live in ``widgets.py``.
"""
from __future__ import annotations

import argparse
import html
import logging
import threading
import time
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
                               QProgressBar, QScrollArea, QSlider, QVBoxLayout, QWidget)


class NoWheelSlider(QSlider):
    """Scrolling the page must never change the output level by accident."""
    def wheelEvent(self, e):
        """Ignore the wheel so scrolling the page never changes the output level."""
        e.ignore()


from . import __version__
from .core import (MAX_BOOST_DB, load_prefs, save_prefs, list_backups, read_small_file, validate_bands, FLAT, LIBRARY, NUM_BANDS, VOL_MAX, VOL_MIN, VOL_ZERO, Preset,
                   atomic_write, clean_text, decode_text, load_user_presets, parse_apo, peak_boost, save_backup,
                   save_user_presets, suggest_volume_db, to_apo, volume_db, volume_step)
log = logging.getLogger("arcpeq")
from .logo import app_icon, logo_image
from .device import (DemoBackend, DeviceError, RealBackend, Unsupported, add_user_device, parse_vidpid)
from .widgets import ACCENT, BandCard, CurveWidget

LOUD_DB = 9.0


def _tick_image() -> str:
    """A small white check mark for the on/off boxes (Qt style sheets can only use an image file for it)."""
    import os
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    try:
        from .core import data_dir
        path = os.path.join(str(data_dir()), "arcpeq_tick.png")
    except OSError:
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="arcpeq"), "tick.png")
    img = QImage(16, 16, QImage.Format_ARGB32)
    img.fill(0)
    g = QPainter(img)
    g.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("white"), 2.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    g.setPen(pen)
    g.drawPolyline([QPointF(3.5, 8.5), QPointF(6.8, 11.8), QPointF(12.5, 4.8)])
    g.end()
    img.save(path)
    return path.replace("\\", "/")


TICK = _tick_image()

# One global Qt style sheet (dark theme). Dialogs are styled explicitly so they stay readable on macOS.
STYLE = f"""
* {{ color: #e6e8ee; }}
QMainWindow, QWidget#root {{ background: #0e1015; }}
QWidget#sidebar {{ background: #12151c; border-right: 1px solid #1f2430; }}
QLabel#h1 {{ font-size: 17pt; font-weight: 700; }}
QLabel#sub, QLabel#caption {{ color: #9aa3b8; }}
QLabel#section {{ color: #98a0b5; font-size: 8pt; font-weight: 700; padding: 14px 16px 4px 16px; }}
QLabel#gainLabel {{ font-size: 15pt; font-weight: 700; }}
QLabel#cardTitle {{ font-weight: 600; }}
QLabel#warn {{ color: #ffb454; }}
QListWidget {{ background: transparent; border: 1px solid transparent; outline: none; }}
QListWidget QLabel {{ background: transparent; }}
QListWidget::item {{ border-radius: 10px; margin: 1px 8px; }}
QListWidget::item:hover {{ background: #1a1e28; }}
QListWidget::item:selected {{ background: #232a3d; }}
QListWidget:focus {{ border: 1px solid transparent; }}
QListWidget::item:selected:active {{ background: #2a3350; }}
QFrame#bandCard {{ background: #141821; border: 1px solid #1f2430; border-radius: 14px; }}
QFrame#bandCard[selected="true"] {{ border: 1px solid {ACCENT}; background: #171c28; }}
QFrame#bandCard[off="true"] QLabel#gainLabel {{ color: #7a8296; }}
QCheckBox:focus {{ color: white; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 2px solid #8a93aa; border-radius: 5px; background: #0e1015; }}
QCheckBox::indicator:hover {{ border-color: #c9d0e4; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({TICK}); }}
QCheckBox:focus::indicator {{ border-color: white; }}
QPushButton::menu-indicator {{ image: none; width: 0; }}
QProgressBar {{ background: #1b202b; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
QSlider[unknown="true"]::sub-page:horizontal {{ background: #2a3140; }}
QSlider[unknown="true"]::handle:horizontal {{ background: #8a93aa; }}
QWidget#mainPanel {{ background: transparent; }}
QFrame#intro {{ background: #172036; border: 1px solid #2c3d66; border-radius: 12px; }}
QFrame#devCard {{ background: #171b24; border: 1px solid #232937; border-radius: 12px; }}
QPushButton {{ background: #1b202b; border: 1px solid #2a3140; border-radius: 9px; padding: 8px 14px; }}
QPushButton:hover {{ background: #222836; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton:disabled {{ color: #7a8296; background: #151922; border-color: #1f2430; }}
QPushButton#primary {{ background: #4a68e0; border: none; color: white; font-weight: 700; padding: 9px 22px; }}
QPushButton#primary:hover {{ background: #4a68d8; }}
QPushButton#primary:focus {{ border: 2px solid white; }}
QPushButton#primary:disabled {{ background: #232a44; color: #8791b0; }}
QComboBox, QSpinBox, QDoubleSpinBox {{ background: #1b202b; border: 1px solid #2a3140; border-radius: 7px; padding: 3px 7px; min-height: 20px; }}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QComboBox QAbstractItemView {{ background: #1b202b; selection-background-color: #2a3350; border: 1px solid #2a3140; }}
QSlider::groove:horizontal {{ height: 4px; background: #2a3140; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:focus {{ border: 2px solid {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2a3140; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; height: 0; }}
QDialog, QMessageBox, QInputDialog {{ background: #171b24; }}
QMessageBox QLabel, QInputDialog QLabel {{ color: #e6e8ee; background: transparent; }}
QInputDialog QLineEdit {{ background: #0e1015; border: 1px solid #2a3140; border-radius: 6px; padding: 6px; }}
QMenu {{ background: #1b202b; border: 1px solid #2a3140; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: #2a3350; }}
"""


class Controller(QObject):
    """Owns USB I/O. Slow work runs on one worker thread; results return to the UI thread through signals
    that are connected to bound methods of QObjects (so Qt queues them onto the right thread)."""
    devices = Signal(list)
    loaded = Signal(object, str, object, bool)       # info, firmware, bands, silent
    applied = Signal(object, object, object)         # info, bands read back, bands that were sent
    applyFailed = Signal(object, str)                # info, message (device state is now uncertain)
    unsupported = Signal(object, str)                # device holds settings we can't show
    volumeSet = Signal(object, int)
    saved = Signal(object, object, object, object)   # info (as re-attached), bands read back, bands sent, result dict
    rebooted = Signal(object)                        # the device restarted: its output level is now unknown
    failed = Signal(str, bool)                       # message, offer retry
    busy = Signal(bool)
    _finished = Signal()

    def __init__(self, backend):
        super().__init__()
        self.backend = backend
        self.pool = ThreadPoolExecutor(max_workers=1)
        self._busy = False
        self._sig = None
        self.cancel = threading.Event()              # set on quit so waiting loops end promptly
        self.settle = 1.5                            # seconds to let the Arc finish restarting before we look for it
        self.paused = False                          # True while a dialog is open: no rescans, nothing moves under it
        self._finished.connect(self._on_finished)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.rescan)
        self.timer.start(1200)
        QTimer.singleShot(0, self.rescan)

    def rescan(self, force=False):
        """Look for plugged-in devices (skipped while busy or paused). Emits ``devices`` when the list changed."""
        if (self._busy or self.paused) and not force:
            return
        try:
            found = self.backend.list_devices()
        except Exception:                                   # noqa: BLE001  (enumeration must never kill the UI)
            found = []
        sig = [(d.key, d.paths) for d in found]             # a re-plugged device keeps its key but gets new paths
        if sig != self._sig:
            self._sig = sig
            self.devices.emit(found)

    def _on_finished(self):
        """Runs on the UI thread when a background job ends; clears the busy flag."""
        self._busy = False
        self.busy.emit(False)

    def _run(self, fn, retry=False, quiet=False):
        """Run ``fn`` on the worker thread (one job at a time).

        DeviceErrors become friendly ``failed`` signals; ``retry`` only tells the UI to offer a Try again button;
        ``quiet`` (background refresh) logs errors instead of showing them.
        """
        if self._busy or self.cancel.is_set():
            return False
        self._busy = True
        self.busy.emit(True)

        def job():
            try:
                fn()
            except DeviceError as e:
                if quiet:
                    log.info("background job failed: %s", e)
                else:
                    self.failed.emit(str(e), retry)
            except Exception as e:                          # noqa: BLE001
                logging.getLogger("arcpeq").exception("job failed")
                if not quiet:
                    self.failed.emit(f"Unexpected error: {e}", retry)
            finally:
                self._finished.emit()
        self.pool.submit(job)
        return True

    def read(self, info, silent=False):
        """Read the device's bands. ``silent`` = background refresh, so errors stay quiet."""
        def fn():
            s = self.backend.open(info)
            try:
                fw = s.version()
                try:
                    bands = s.read_bands()
                except Unsupported as e:
                    try:
                        from .core import save_raw_backup
                        save_raw_backup(info.name, f"firmware {fw}: {e}")
                    except OSError:
                        pass
                    self.unsupported.emit(info, str(e))
                    return
                self.loaded.emit(info, fw, bands, silent)
            finally:
                s.close()
        return self._run(fn, retry=not silent, quiet=silent)

    def apply(self, info, bands, expect_fw=None, volume_step=None):
        """Live apply: write the EQ, read it back, optionally set the staged output level. No flash save, so the
        Arc does not restart and the change is lost when it is unplugged (use save() to keep it)."""
        sent = list(bands)

        def fn():
            s = self.backend.open(info, expect_fw)
            try:
                try:
                    s.write_bands(sent)
                    verified = s.read_bands()
                except DeviceError as e:
                    self.applyFailed.emit(info, str(e))
                    return
                self.applied.emit(info, verified, sent)
                if volume_step is not None:
                    s.set_volume(volume_step)
                    self.volumeSet.emit(info, volume_step)
            finally:
                s.close()
        return self._run(fn)

    def save(self, info, bands, expect_fw=None, restore_step=None):
        """Write the EQ, commit it to the Arc's memory, then wait for the Arc to restart (it drops and re-attaches its
        USB connection, and its output level resets), read it back and put the output level back."""
        sent = list(bands)

        def fn():
            res = dict(info=info, restart_seen=False, save_err=None, level_restored=None, level_error=None)
            s = self.backend.open(info, expect_fw)
            try:
                try:
                    s.write_bands(sent)
                except DeviceError as e:
                    self.applyFailed.emit(info, str(e))
                    return
                try:
                    s.save()
                except DeviceError as e:                 # it may restart before it can answer; the read-back decides
                    res["save_err"] = str(e)
                    log.warning("save: %s", e)
            finally:
                s.close()
            res["restart_seen"] = self._await_restart(info)
            if res["restart_seen"]:
                self.rebooted.emit(info)                 # its output level is unknown from this moment
                self._sleep(self.settle)
            s2, d = self._await_session(info, expect_fw)
            if s2 is None:
                self.applyFailed.emit(info, "The device didn't come back after saving. Unplug it, plug it back in, "
                                            "and press ↻ to check what it holds")
                return
            res["info"] = d
            try:
                verified = s2.read_bands()
                if res["restart_seen"] and restore_step is not None:
                    try:
                        s2.set_volume(restore_step)
                        res["level_restored"] = restore_step
                        self.volumeSet.emit(d, restore_step)
                    except DeviceError as e:
                        res["level_error"] = str(e)
            except Unsupported as e:
                self.unsupported.emit(d, str(e))
                return
            except DeviceError as e:
                self.applyFailed.emit(d, str(e))
                return
            finally:
                s2.close()
            self.saved.emit(d, verified, sent, res)
        return self._run(fn)

    def _sleep(self, seconds):
        """Sleep in small slices so closing the app is never held up by a long wait."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.cancel.is_set():
            time.sleep(0.05)

    def _present(self, info):
        """Return the matching device if it is plugged in right now, ``[]`` if gone, ``None`` if we couldn't tell."""
        try:
            return [d for d in self.backend.list_devices() if d.key == info.key]
        except Exception:                                   # noqa: BLE001
            return None

    def _await_restart(self, info, timeout_s=6.0):
        """True once the device has visibly gone away (or re-attached with new paths)."""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end and not self.cancel.is_set():
            here = self._present(info)
            if here is not None and (not here or here[0].paths != info.paths):
                return True
            self._sleep(0.2)
        return False

    def _await_session(self, info, expect_fw, deadline_s=15.0):
        """Open a session as soon as the device answers again: (Session, DeviceInfo) or (None, None)."""
        end = time.monotonic() + deadline_s
        while time.monotonic() < end and not self.cancel.is_set():
            for d in self._present(info) or []:
                try:
                    return self.backend.open(d, expect_fw), d
                except DeviceError as e:
                    log.info("waiting for device: %s", e)
            self._sleep(0.4)
        return None, None

    def set_volume(self, info, step, expect_fw=None):
        """Send only the output level (the DAC volume register) to the device."""
        def fn():
            s = self.backend.open(info, expect_fw)
            try:
                s.set_volume(step)
                self.volumeSet.emit(info, step)
            finally:
                s.close()
        return self._run(fn)

    def shutdown(self):
        """Stop polling and background work; called when the window closes."""
        self.timer.stop()
        self.cancel.set()
        self.pool.shutdown(wait=True)


class MainWindow(QMainWindow):
    """The main window. Holds the staged sound, the per-device state, and every user action."""
    def __init__(self, backend, demo=False):
        super().__init__()
        self.setWindowTitle("Arc PEQ" + ("  (demo)" if demo else ""))
        self.resize(1240, 800)
        self.setMinimumSize(1000, 680)

        self.user_presets, warnings = load_user_presets()
        self.bands = list(LIBRARY[0].bands)      # the sound on screen (a draft until Apply)
        self.device_bands = None         # what the device holds right now (None = not read yet)
        self.original = {}               # device key -> bands when first read this session
        self.confirmed = set()           # device keys the user OK'd writing to (unverified / new firmware)
        self.devs = []
        self.current = None
        self.firmware = ""
        self.busy = False
        self.selected = 0
        self._pill_key = None
        self.uncertain = set()           # device keys whose state is unknown after a failed write
        self.unsup = {}                  # device key -> reason we can't show its settings
        self.vol_pending = None          # output-level step staged on the slider, not yet sent
        self.unsaved = set()             # device keys running a live (Apply) sound that is not saved to its memory
        self.saved_keys = set()          # device keys we saved to this session
        self.job = None                  # what the controller is doing: apply / save / volume / read
        self._save_warned = False
        self._sug_shown = False
        self.stash = {}                  # band index -> last non-zero gain (for the on/off switch)
        self.volume = {}                 # device key -> step last set by this app (the Arc can't report it)
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self._stuck)

        self.ctl = Controller(backend)
        self.ctl.devices.connect(self.on_devices)
        self.ctl.loaded.connect(self.on_loaded)
        self.ctl.applied.connect(self.on_applied)
        self.ctl.applyFailed.connect(self.on_apply_failed)
        self.ctl.unsupported.connect(self.on_unsupported)
        self.ctl.volumeSet.connect(self.on_volume_set)
        self.ctl.rebooted.connect(self.on_rebooted)
        self.ctl.saved.connect(self.on_saved)
        self.ctl.failed.connect(self.on_failed)
        self.ctl.busy.connect(self.on_busy)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        outer.addWidget(self._build_main(), 1)

        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+Return"))
        act.triggered.connect(self.apply)
        self.addAction(act)
        QApplication.instance().applicationStateChanged.connect(self.on_app_state)

        self.refresh_presets()
        self.refresh_all()
        if warnings:
            self.flash(" ".join(warnings), True, sticky=True)

    # ---------------------------------------------------------------- layout
    def _build_sidebar(self):
        """Left column: logo, preset list, device picker and output-level slider."""
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(280)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(0, 18, 0, 14)
        lay.setSpacing(0)
        brand = QWidget()
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(20, 0, 16, 0)
        bl.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(QPixmap.fromImage(logo_image(88)).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        mark.setFixedSize(40, 40)
        mark.setAccessibleName("Arc PEQ logo")
        bl.addWidget(mark)
        title = QLabel("Arc PEQ")
        title.setObjectName("h1")
        bl.addWidget(title, 1)
        lay.addWidget(brand)
        sub = QLabel("Sound presets for your IEM dongle")
        sub.setObjectName("sub")
        sub.setContentsMargins(20, 2, 0, 6)
        lay.addWidget(sub)

        self.list = QListWidget()
        self.list.setAccessibleName("Presets")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.preset_menu)
        self.list.itemClicked.connect(self.on_preset_clicked)
        self.list.itemActivated.connect(self.on_preset_clicked)
        QShortcut(QKeySequence("F2"), self.list, activated=self.rename_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence("Delete"), self.list, activated=self.delete_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence("Backspace"), self.list, activated=self.delete_selected, context=Qt.WidgetShortcut)
        lay.addWidget(self.list, 1)

        card = QFrame()
        card.setObjectName("devCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(6)
        cap = QLabel("DEVICE")
        cap.setObjectName("section")
        cap.setStyleSheet("padding: 0;")
        cl.addWidget(cap)
        row = QHBoxLayout()
        self.picker = QComboBox()
        self.picker.setAccessibleName("Choose device")
        self.picker.activated.connect(self.on_pick)
        row.addWidget(self.picker, 1)
        self.rescan_btn = QPushButton("↻")
        self.rescan_btn.setToolTip("Look for devices again")
        self.rescan_btn.setAccessibleName("Rescan devices")
        self.rescan_btn.setFixedWidth(38)
        self.rescan_btn.clicked.connect(lambda: self.ctl.rescan(force=True))
        row.addWidget(self.rescan_btn)
        cl.addLayout(row)
        self.dev_sub = QLabel()
        self.dev_sub.setObjectName("sub")
        self.dev_sub.setTextFormat(Qt.PlainText)
        self.dev_sub.setWordWrap(True)
        cl.addWidget(self.dev_sub)

        vrow = QHBoxLayout()
        vcap = QLabel("OUTPUT LEVEL")
        vcap.setObjectName("section")
        vcap.setStyleSheet("padding: 8px 0 0 0;")
        self.vol_label = QLabel("0.0 dB")
        self.vol_label.setStyleSheet("padding-top: 8px; font-weight: 700;")
        vrow.addWidget(vcap)
        vrow.addStretch()
        vrow.addWidget(self.vol_label)
        cl.addLayout(vrow)
        self.vol = NoWheelSlider(Qt.Horizontal)
        self.vol.setRange(VOL_MIN, VOL_MAX)
        self.vol.setValue(VOL_ZERO)
        self.vol.setAccessibleName("Output level in decibels")
        self.vol.setToolTip("The Arc's DAC volume, -6 to +3 dB in 1.5 dB steps. Takes effect when you press Apply.")
        self.vol.valueChanged.connect(self.on_volume_moved)
        cl.addWidget(self.vol)
        self.vol_note = QLabel()
        self.vol_note.setObjectName("sub")
        self.vol_note.setWordWrap(True)
        self.vol_note.setTextFormat(Qt.PlainText)
        cl.addWidget(self.vol_note)
        self.vol_suggest = QPushButton()
        self.vol_suggest.clicked.connect(self.use_suggested_volume)
        cl.addWidget(self.vol_suggest)
        wrap = QVBoxLayout()
        wrap.setContentsMargins(14, 0, 14, 0)
        wrap.addWidget(card)
        lay.addLayout(wrap)
        return side

    def _build_main(self):
        """Right column: title, graph, five band cards and the pinned Apply / Save bar."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(26, 20, 26, 6)
        lay.setSpacing(12)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("h1")
        self.title.setTextFormat(Qt.PlainText)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("sub")
        self.subtitle.setTextFormat(Qt.PlainText)
        self.subtitle.setWordWrap(True)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        head.addLayout(col, 1)
        self.pill = QLabel()
        self.pill.setTextFormat(Qt.PlainText)
        head.addWidget(self.pill, 0, Qt.AlignTop)
        self.save_btn = QPushButton("Save as preset")
        self.save_btn.clicked.connect(self.save_preset)
        head.addWidget(self.save_btn, 0, Qt.AlignTop)
        lay.addLayout(head)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)                         # indeterminate: shown while the Arc is being written
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 1)                         # idle: an empty track, so the layout never jumps
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        self.intro = QFrame()
        self.intro.setObjectName("intro")
        il = QHBoxLayout(self.intro)
        il.setContentsMargins(14, 8, 10, 8)
        t = QLabel("New here?  1 · Pick a preset on the left    2 · Drag the dots to fine-tune    "
                   "3 · Apply to try it, Save to Arc to keep it")
        t.setTextFormat(Qt.PlainText)
        t.setWordWrap(True)
        il.addWidget(t, 1)
        got = QPushButton("Got it")
        got.clicked.connect(self.dismiss_intro)
        il.addWidget(got)
        self.intro.setVisible(not load_prefs().get("intro_seen"))
        lay.addWidget(self.intro)

        self.curve = CurveWidget()
        self.curve.bandEdited.connect(self.on_band_edited)
        self.curve.bandSelected.connect(self.on_band_selected)
        lay.addWidget(self.curve, 1)

        hint = QLabel("Drag the numbered dots, or click the graph and use the arrow keys · "
                      "scroll over it to change width (Q) · Apply tries the sound on the Arc now · Save to Arc keeps it after unplugging")
        hint.setObjectName("caption")
        hint.setWordWrap(True)
        hint.setMinimumHeight(hint.fontMetrics().lineSpacing() * 2 + 4)     # always room for two lines
        lay.addWidget(hint)
        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setTextFormat(Qt.PlainText)
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.cards = []
        for i in range(NUM_BANDS):
            c = BandCard(i)
            c.changed.connect(self.on_band_edited)
            c.clicked.connect(self.on_band_selected)
            c.toggled.connect(self.on_band_toggled)
            self.cards.append(c)
            row.addWidget(c, 1)
        lay.addLayout(row)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        msgs = QVBoxLayout()
        msgs.setSpacing(2)
        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        msgs.addWidget(self.message)
        bar.addLayout(msgs, 1)
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.clicked.connect(self.read)
        self.retry_btn.hide()
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setToolTip("Go back to what is on the device right now")
        self.revert_btn.clicked.connect(self.revert)
        self.more = QPushButton("More  ▾")
        menu = QMenu(self)
        menu.addAction("Reset to Flat", lambda: self.load_preset(FLAT))
        menu.addAction("Restore original sound (from when you connected)", self.restore_original)
        menu.addAction("Restore from a saved backup…", self.restore_backup)
        menu.addSeparator()
        menu.addAction("Save as preset…", self.save_preset)
        menu.addAction("Import EQ file…", self.import_file)
        menu.addAction("Export current sound…", self.export_file)
        menu.addSeparator()
        menu.addAction("Read from device", self.read)
        menu.addAction("Add device by ID…", self.add_device)
        self.more.setMenu(menu)
        self.store_btn = QPushButton("Save to Arc")
        self.store_btn.clicked.connect(self.save_to_device)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self.apply)
        for b in (self.retry_btn, self.revert_btn, self.more, self.store_btn, self.apply_btn):
            bar.addWidget(b)
        content = w
        content.setObjectName("mainPanel")
        content.setMinimumWidth(1)                   # width may squeeze (as before); only height scrolls
        scroll = QScrollArea()                       # below the layout's natural height the panel scrolls instead of
        scroll.setWidgetResizable(True)              # squeezing its widgets on top of each other
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        barw = QWidget()                             # the action bar stays pinned at the bottom
        bl = QVBoxLayout(barw)
        bl.setContentsMargins(26, 4, 26, 16)
        bl.addLayout(bar)
        box = QWidget()
        bx = QVBoxLayout(box)
        bx.setContentsMargins(0, 0, 0, 0)
        bx.setSpacing(0)
        bx.addWidget(scroll, 1)
        bx.addWidget(barw)
        return box

    # ---------------------------------------------------------------- presets
    def all_presets(self):
        """Built-in presets, Flat, then the user's own presets."""
        return LIBRARY + [FLAT] + self.user_presets

    def current_preset(self):
        """The preset whose bands equal what is on screen, or None for a custom sound."""
        return next((p for p in self.all_presets() if p.bands == self.bands), None)

    def unsaved_custom(self):
        """True when the screen holds a hand-tuned sound that is neither a preset nor on the device."""
        return self.current_preset() is None and self.device_bands != self.bands

    def refresh_presets(self):
        """Rebuild the preset list (built-ins, utility, then the user's saved presets)."""
        self.list.blockSignals(True)
        self.list.clear()

        def header(text):
            it = QListWidgetItem()
            it.setFlags(Qt.NoItemFlags)
            lab = QLabel(text)
            lab.setObjectName("section")
            it.setSizeHint(lab.sizeHint())
            self.list.addItem(it)
            self.list.setItemWidget(it, lab)

        def add(p):
            it = QListWidgetItem()
            it.setData(Qt.UserRole, p.id)
            it.setData(Qt.AccessibleTextRole, f"{p.name}. {p.detail}")
            lab = QLabel(f"<b>{html.escape(p.name)}</b><br>"
                         f"<span style='color:#9aa3b8;font-size:8pt'>{html.escape(p.detail)}</span>")
            lab.setTextFormat(Qt.RichText)
            lab.setContentsMargins(10, 3, 8, 3)
            lab.setAttribute(Qt.WA_TransparentForMouseEvents)
            lab.setWordWrap(True)
            it.setSizeHint(QSize(0, 52))
            self.list.addItem(it)
            self.list.setItemWidget(it, lab)

        header("PRESETS")
        for p in LIBRARY:
            add(p)
        header("UTILITY")
        add(FLAT)
        if self.user_presets:
            header("MY PRESETS")
            for p in self.user_presets:
                add(p)
        self.list.blockSignals(False)
        self.sync_selection()

    def sync_selection(self):
        """Highlight the list row that matches the sound on screen."""
        cur = self.current_preset()
        self.list.blockSignals(True)
        self.list.clearSelection()
        for i in range(self.list.count()):
            it = self.list.item(i)
            if cur and it.data(Qt.UserRole) == cur.id:
                it.setSelected(True)
                self.list.scrollToItem(it)
        self.list.blockSignals(False)

    def msg(self, title, text, yes="Continue", warn=True):
        """Plain-text confirmation dialog (device-supplied names can't inject markup). True if confirmed."""
        box = QMessageBox(QMessageBox.Warning if warn else QMessageBox.Question, title, text, parent=self)
        box.setTextFormat(Qt.PlainText)
        ok = box.addButton(yes, QMessageBox.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        self.ctl.paused = True
        try:
            box.exec()
        finally:
            self.ctl.paused = False
        return box.clickedButton() is ok

    def choose(self, title, text, labels):
        """Plain-text dialog with several action buttons plus Cancel. Returns the chosen index or None."""
        box = QMessageBox(QMessageBox.Warning, title, text, parent=self)
        box.setTextFormat(Qt.PlainText)
        btns = [box.addButton(t, QMessageBox.AcceptRole) for t in labels]
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        self.ctl.paused = True
        try:
            box.exec()
        finally:
            self.ctl.paused = False
        clicked = box.clickedButton()
        return btns.index(clicked) if clicked in btns else None

    def note(self, title, text):
        """Show a plain-text information box (pauses device polling while open)."""
        box = QMessageBox(QMessageBox.Information, title, text, parent=self)
        box.setTextFormat(Qt.PlainText)
        self.ctl.paused = True
        try:
            box.exec()
        finally:
            self.ctl.paused = False

    def confirm_discard(self):
        """True if it's fine to replace the sound on screen."""
        if not self.unsaved_custom():
            return True
        box = QMessageBox(QMessageBox.Question, "Unsaved changes",
                          "You've tuned a sound that isn't saved as a preset.", parent=self)
        box.setInformativeText("Save it before switching, or discard your changes?")
        save = box.addButton("Save as preset…", QMessageBox.AcceptRole)
        discard = box.addButton("Discard changes", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setTextFormat(Qt.PlainText)
        self.ctl.paused = True
        try:
            box.exec()
        finally:
            self.ctl.paused = False
        if box.clickedButton() is save:
            self.save_preset()
            return not self.unsaved_custom()
        return box.clickedButton() is discard

    def on_preset_clicked(self, item):
        """A preset row was clicked: load it onto the screen (not onto the device)."""
        pid = item.data(Qt.UserRole)
        p = next((x for x in self.all_presets() if x.id == pid), None)
        if p:
            self.load_preset(p)
        else:
            self.sync_selection()

    def load_preset(self, preset):
        """Put a preset's bands on screen after checking the user won't lose unsaved work."""
        if not self.confirm_discard():
            self.sync_selection()
            return
        self.bands = list(preset.bands)
        self.refresh_all()

    def _selected_user_preset(self):
        """The highlighted preset if it is one of the user's own, else None."""
        for it in self.list.selectedItems():
            pid = it.data(Qt.UserRole)
            return next((x for x in self.user_presets if x.id == pid), None)
        return None

    def rename_selected(self):
        """F2 shortcut: rename the highlighted user preset."""
        p = self._selected_user_preset()
        if p:
            self.rename_preset(p)

    def delete_selected(self):
        """Delete shortcut: delete the highlighted user preset (after confirming)."""
        p = self._selected_user_preset()
        if p:
            self.delete_preset(p)

    def preset_menu(self, pos):
        """Right-click menu for a preset row (load, update, rename, duplicate, export, delete)."""
        item = self.list.itemAt(pos)
        if not item or item.data(Qt.UserRole) is None:
            return
        pid = item.data(Qt.UserRole)
        p = next((x for x in self.user_presets if x.id == pid), None)
        m = QMenu(self)
        if p:
            m.addAction("Load", lambda: self.load_preset(p))
            m.addAction("Update with current sound", lambda: self.update_preset(p))
            m.addAction("Rename…\tF2", lambda: self.rename_preset(p))
            m.addAction("Duplicate", lambda: self.duplicate_preset(p))
            m.addAction("Export…", lambda: self.export_file(p))
            m.addSeparator()
            m.addAction("Delete…\tDel", lambda: self.delete_preset(p))
        else:
            b = next((x for x in self.all_presets() if x.id == pid), None)
            if not b:
                return
            m.addAction("Load", lambda: self.load_preset(b))
            m.addAction("Duplicate to My Presets", lambda: self.duplicate_preset(b))
        m.exec(self.list.mapToGlobal(pos))

    def _persist(self, message):
        """Write the user's presets to disk, then refresh the UI and show ``message``."""
        try:
            save_user_presets(self.user_presets)
        except OSError as e:
            self.flash(f"Couldn't save your presets: {e}", True)
            return
        self.refresh_presets()
        self.refresh_all()
        self.flash(message)

    def save_preset(self):
        """Ask for a name and store the on-screen sound as a user preset (or offer to update one of that name)."""
        name, ok = QInputDialog.getText(self, "Save preset", "Name for this sound:")
        name = clean_text(name)
        if not (ok and name):
            return
        existing = next((p for p in self.user_presets if p.name.lower() == name.lower()), None)
        if existing:
            self.update_preset(existing)
            return
        self.user_presets.append(Preset(uuid.uuid4().hex, name, "Custom preset", list(self.bands), False))
        self._persist(f"Saved “{name}” under My Presets")

    def update_preset(self, preset):
        """Replace a user preset's bands with the sound on screen."""
        if self.msg("Update preset", f"Replace “{preset.name}” with the sound on screen?", "Replace", warn=False):
            preset.bands = list(self.bands)
            self._persist(f"Updated “{preset.name}”")

    def rename_preset(self, preset):
        """Rename a user preset."""
        name, ok = QInputDialog.getText(self, "Rename preset", "New name:", text=preset.name)
        name = clean_text(name)
        if ok and name:
            preset.name = name
            self._persist("Renamed")

    def duplicate_preset(self, preset):
        """Copy a preset (built-in or user) into the user's presets."""
        self.user_presets.append(Preset(uuid.uuid4().hex, clean_text(f"{preset.name} copy"), "Custom preset",
                                        list(preset.bands), False))
        self._persist(f"Duplicated “{preset.name}”")

    def delete_preset(self, preset):
        """Delete a user preset after confirmation."""
        if self.msg("Delete preset", f"Delete “{preset.name}”? This can't be undone.", "Delete"):
            self.user_presets = [p for p in self.user_presets if p.id != preset.id]
            self._persist("Deleted")

    def export_file(self, preset=None):
        """Save a preset (or the sound on screen) as an Equalizer APO / AutoEq text file."""
        bands = preset.bands if isinstance(preset, Preset) else self.bands
        cur = self.current_preset()
        name = preset.name if isinstance(preset, Preset) else (cur.name if cur else "My sound")
        path, _ = QFileDialog.getSaveFileName(self, "Export EQ", f"{name}.txt", "EQ text (*.txt)")
        if not path:
            return
        try:
            from pathlib import Path
            atomic_write(Path(path), to_apo(bands, name))
            self.flash("Exported.")
        except OSError as e:
            self.flash(f"Couldn't export: {e}", True)

    def import_file(self):
        """Load an Equalizer APO / AutoEq text file onto the screen (size-limited, strictly parsed)."""
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import EQ", "", "EQ text (*.txt);;All files (*)")
        if not path:
            return
        try:
            raw = read_small_file(path)
            bands, notes = parse_apo(decode_text(raw))
        except (OSError, ValueError) as e:
            self.flash(str(e), True)
            return
        self.bands = bands
        self.refresh_all()
        extra = []
        if peak_boost(bands) > 6:
            extra.append(f"Note: this file boosts by up to +{peak_boost(bands):.1f} dB. Check the output level first.")
        self.flash(" ".join(["Imported."] + notes + extra + ["Press Apply to try it."]), bool(extra))

    # ---------------------------------------------------------------- state / chrome
    @property
    def dirty(self):
        """True when the screen differs from what the device holds."""
        return self.device_bands is None or self.device_bands != self.bands

    @property
    def connected(self):
        """True when a device is selected."""
        return self.current is not None

    def on_band_toggled(self, i, on):
        """A band's On switch: Off sets gain to 0 (remembering it), On restores the remembered gain."""
        b = self.bands[i]
        if on:
            self.on_band_edited(i, b.with_(gain=self.stash.get(i) or 3.0))
        else:
            if b.gain != 0:
                self.stash[i] = b.gain             # remember it so switching back on restores it
            self.on_band_edited(i, b.with_(gain=0.0))

    def on_band_edited(self, i, band):
        """A band was changed (by the graph or a card): store it and redraw everything."""
        if self.bands[i] == band:
            return
        if band.gain != 0:
            self.stash[i] = band.gain
        self.bands[i] = band
        self.selected = i
        self.refresh_all()

    def on_band_selected(self, i):
        """Make band ``i`` the selected one."""
        if i != self.selected:
            self.selected = i
            self.refresh_all()

    def refresh_all(self):
        """Redraw graph, cards and status from ``self.bands``. Resets to Flat if values are ever out of range."""
        try:                                   # last line of defence: never draw or send invalid values
            validate_bands(self.bands)
        except ValueError:
            self.bands = list(FLAT.bands)
            self.flash("Some values were out of range, so the sound was reset to Flat.", True)
        self.curve.set_bands(self.bands, self.selected, self.device_bands)
        self._sync_curve_level()
        for i, c in enumerate(self.cards):
            if c.band != self.bands[i] or c.property("selected") != (i == self.selected):
                c.set_band(self.bands[i], i == self.selected)
        cur = self.current_preset()
        self.title.setText(cur.name if cur else "Custom sound")
        self.subtitle.setText(cur.detail if cur else "Your own tweaks. Press “Save as preset” to keep them.")
        self.sync_selection()
        self.refresh_chrome()

    def refresh_chrome(self):
        """Update everything that isn't the curve: status pill, button states, warnings, volume widgets."""
        self._sync_curve_level()
        d = self.current
        key = d.key if d else None
        if not self.connected:
            text, col = "No device selected", "#9aa3b8"
        elif self.busy:
            text, col = (("●  Saving to your Arc… don't unplug", "#ffb454") if self.job == "save" else
                         ("●  Working…", "#ffb454"))
        elif self.dirty or self.vol_dirty:
            text, col = "●  Not applied yet", "#ffb454"
        elif key in self.uncertain:
            text, col = "●  State unknown: press Read", "#ffb454"
        elif key in self.unsaved:
            text, col = "●  Live on your Arc, not saved", "#7fb2ff"
        elif key in self.saved_keys:
            text, col = "●  Saved on your Arc", "#42d392"
        else:
            text, col = "●  Active on your Arc", "#42d392"
        if (text, col) != self._pill_key:
            self._pill_key = (text, col)
            self.pill.setText(text)
            self.pill.setStyleSheet(f"color:{col}; background:#171b24; border:1px solid #232937;"
                                    "border-radius:12px; padding:5px 12px;")
        self.progress.setRange(0, 0 if self.busy else 1)    # busy = moving, idle = empty track
        self.progress.setValue(0)
        can = self.connected and not self.busy
        shortcut = QKeySequence("Ctrl+Return").toString(QKeySequence.NativeText)
        self.apply_btn.setText("Applying…" if self.busy and self.job in ("apply", "volume") else "Apply")
        self.apply_btn.setEnabled(can and (self.dirty or self.vol_dirty))
        self.apply_btn.setToolTip(
            f"Try this sound on the Arc now ({shortcut}). It stays until you unplug the Arc; use Save to Arc to keep it."
            if self.apply_btn.isEnabled() else
            "Connect a device first" if not self.connected else
            "Working on the device…" if self.busy else
            "Nothing to apply: the device already has this sound and level")
        reason = self.save_block_reason()
        self.store_btn.setText("Saving…" if self.busy and self.job == "save" else "Save to Arc")
        self.store_btn.setEnabled(can and reason is None)
        self.store_btn.setToolTip(
            "Keep this sound on the Arc after you unplug it. The Arc restarts for a few seconds (sound may briefly "
            "come from your computer's speakers) and its output level resets; the app puts your level back."
            if self.store_btn.isEnabled() else reason or "Working on the device…")
        self.revert_btn.setEnabled((self.device_bands is not None and self.dirty or self.vol_dirty) and not self.busy)
        self.more.setEnabled(not self.busy)
        self.rescan_btn.setEnabled(not self.busy)
        self.picker.setEnabled(bool(self.devs) and not self.busy)

        warn_note = False
        if d:
            fw = f" · firmware {self.firmware}" if self.firmware else ""
            note = ""
            if d.key in self.uncertain:
                note = "\n⚠ A write failed, so what's on the device is unknown. Press Read to check."
            elif d.key in self.unsup:
                note = "\n⚠ This device holds settings the app can't show. Applying will replace them."
            elif not d.verified:
                note = "\n⚠ Not tested with this app. You'll be asked before anything is written."
            elif self.firmware and d.firmwares and self.firmware not in d.firmwares:
                note = "\n⚠ This firmware version hasn't been tested with this app."
            warn_note = bool(note)
            self.dev_sub.setText(f"Connected{fw}{note}")
        elif self.devs:
            self.dev_sub.setText("Choose a device above.")
        else:
            self.dev_sub.setText("No device found. Plug in your IEM dongle and close the web editor if it's open, "
                                 "then press ↻.")
        self.dev_sub.setStyleSheet("color: #ffb454;" if warn_note else "")
        self.sync_volume_widgets()
        boost = max(0.0, self._boost())
        if boost > 6:
            self.warn.setStyleSheet("color: #ffb454;")
            self.warn.setText(f"⚠ Strong boost (+{boost:.1f} dB at the peak). Start at a low volume; if bass crackles, "
                              "lower the output level or Band 1.")
        elif self.connected and self._sug_shown:
            self.warn.setStyleSheet("color: #9aa3b8;")
            self.warn.setText(f"Moderate boost (+{boost:.1f} dB). Lowering the output level gives more headroom.")
        elif self.connected:
            self.warn.setStyleSheet("color: #42d392;")
            self.warn.setText("✓ Boost is in a comfortable range.")
        else:
            self.warn.setText("")
        for c in self.cards:
            c.setEnabled(not self.busy)
        self.curve.setEnabled(not self.busy)

    def save_block_reason(self):
        """None if 'Save to Arc' makes sense right now, else why not."""
        d = self.current
        if not d:
            return "Connect a device first"
        if not d.verified:
            return "Saving to the device's memory is only enabled for devices this app has been tested with"
        if self.firmware and d.firmwares and self.firmware not in d.firmwares:
            return "Saving is only enabled for firmware versions this app has been tested with"
        if self.device_bands is None and d.key not in self.unsup:
            return "Still reading the device"
        if not (self.dirty or d.key in self.unsaved or d.key in self.unsup):
            return "Nothing to save: the Arc already holds this sound"
        return None

    def _boost(self):
        """Largest combined boost (dB) of the sound on screen."""
        return peak_boost(self.bands)

    def flash(self, text, error=False, sticky=False):
        """Show a status-line message; it fades after 5 s unless ``sticky``."""
        self.message.setStyleSheet(f"color:{'#ff8a8a' if error else '#9aa3b8'}")
        self.message.setText(text)
        if not sticky:
            QTimer.singleShot(5000, lambda: self.message.setText("") if self.message.text() == text else None)

    # ---------------------------------------------------------------- devices
    def forget_level(self, key):
        """The Arc can't report its output level and resets it when it restarts or is replugged."""
        self.volume.pop(key, None)
        self.vol_pending = None

    def on_devices(self, devs):
        """The device list changed: refill the picker and pick/keep a device."""
        prev = self.current
        self.devs = list(devs)
        self.picker.blockSignals(True)
        self.picker.clear()
        for d in self.devs:
            self.picker.addItem(d.label, d.key)
            self.picker.setItemData(self.picker.count() - 1, f"{d.label}  ({d.vid:04X}:{d.pid:04X})".replace("<", "").replace(">", ""), Qt.ToolTipRole)
        if not self.devs:
            self.picker.addItem("No device found")
        self.picker.blockSignals(False)

        keys = {d.key: d for d in self.devs}
        if prev and prev.key in keys:
            new = keys[prev.key]
            if new.paths != prev.paths:            # re-attached on its own (replug, or a restart we didn't cause)
                self.forget_level(prev.key)
                self.unsaved.discard(prev.key)
                self.saved_keys.discard(prev.key)
                self.current = new
                self.firmware = ""
                self.device_bands = None
                self.flash(f"“{new.name}” reconnected. Its output level and any unsaved sound were reset; "
                           "reading it again.", True, sticky=True)
                QTimer.singleShot(250, lambda: self.read(silent=True))
            else:
                self.current = new
            self.picker.setCurrentIndex(self.devs.index(self.current))
        else:
            if prev:
                self.forget_level(prev.key)
                self.unsaved.discard(prev.key)
                self.saved_keys.discard(prev.key)
                self.flash(f"“{prev.name}” was disconnected.", True, sticky=True)
            self.current = None
            self.firmware = ""
            self.device_bands = None
            self.vol_pending = None
            self.retry_btn.hide()
            ver = [d for d in self.devs if d.verified]
            if len(ver) == 1 or (ver and len(self.devs) == 1):
                self.select_device(ver[0])
            elif self.devs:
                self.flash("Choose your device from the list to begin.")
        self.refresh_all()

    def on_pick(self, index):
        """The user chose a device in the picker."""
        if 0 <= index < len(self.devs) and self.devs[index] != self.current:
            self.select_device(self.devs[index])

    def select_device(self, info):
        """Switch to a device: clear per-device state and read it."""
        self.vol_pending = None                       # a staged level belongs to one device only
        self.current = info
        self.firmware = ""
        self.device_bands = None
        self.picker.setCurrentIndex(self.devs.index(info))
        self.retry_btn.hide()
        self.message.setText("")
        self.refresh_all()
        QTimer.singleShot(250, self.read)

    def add_device(self):
        """Dialog to add an extra VID:PID the app should look for."""
        text, ok = QInputDialog.getText(
            self, "Add device by ID",
            "Only add devices made by the same company as your Arc. The app will read from it to check that it "
            "answers, and ask before writing.\n\nUSB ID (shown as VID:PID, e.g. 3302:20E9):")
        if not ok:
            return
        try:
            vid, pid = parse_vidpid(text)
        except ValueError as e:
            self.flash(str(e), True)
            return
        add_user_device(vid, pid)
        self.ctl.rescan(force=True)
        self.flash(f"Added {vid:04X}:{pid:04X}. If it is plugged in it will now appear in the device list.")

    # ---------------------------------------------------------------- output level
    def current_volume_step(self):
        """The output-level step last set on the current device (None = unknown)."""
        return self.volume.get(self.current.key) if self.current else None

    @property
    def vol_dirty(self):
        """A level has been chosen on the slider that the device doesn't have yet."""
        return self.vol_pending is not None and self.vol_pending != self.current_volume_step()

    def _sync_curve_level(self):
        """Tell the graph the output level so the whole curve shifts with the slider."""
        known = self.current is not None and self.current.key in self.volume
        self.curve.set_level(volume_db(self.vol.value()),
                             volume_db(self.volume[self.current.key]) if known else 0.0)

    def on_volume_moved(self, step):
        """The level slider moved: stage the new level (nothing is sent until Apply)."""
        self._sync_curve_level()
        self.vol_label.setText(f"{volume_db(step):+.1f} dB")
        if self.current:
            self.vol_pending = step                  # staged only: nothing is sent until Apply
        self.refresh_chrome()

    def confirm_level(self, step):
        """Raising the level when we can't know where it is now: ask first."""
        if self.current.key not in self.volume and step > VOL_ZERO:
            return self.msg("Raise the volume?", "The Arc can't report its current level, so the app can't tell "
                            "whether this is louder than now. Take the earphones out first, or start low.",
                            "Set level")
        return True

    def on_rebooted(self, info):
        """The Arc restarted, so its output level is unknown again."""
        self.forget_level(info.key)                          # the Arc restarted, so its level is unknown again
        self.saved_keys.discard(info.key)
        self.refresh_chrome()

    def on_volume_set(self, info, step):
        """The device confirmed a new output level."""
        self.volume[info.key] = step
        if self.vol_pending == step:
            self.vol_pending = None
        if self.current and self.current.key == info.key and self.job in ("volume", "apply"):
            self.flash(f"Output level set to {volume_db(step):+.1f} dB.")
        self.refresh_chrome()

    def use_suggested_volume(self):
        """Move the slider to the level that leaves headroom for this sound's boost."""
        self.vol.setValue(volume_step(suggest_volume_db(self._boost())))

    def sync_volume_widgets(self):
        """Show the staged level, else the last known level, else 'Unknown'. Never fights an active drag."""
        step = self.current_volume_step()
        staged = self.vol_pending is not None
        if not self.vol.isSliderDown():                      # never fight the mouse while it's being dragged
            shown = self.vol_pending if staged else (VOL_ZERO if step is None else step)
            self.vol.blockSignals(True)
            self.vol.setValue(shown)
            self.vol.blockSignals(False)
        unknown = step is None and not staged
        db = f"{volume_db(self.vol.value()):+.1f} dB"
        self.vol_label.setText("Unknown" if unknown else f"{db} · not applied" if self.vol_dirty else db)
        if self.vol.property("unknown") != unknown:
            self.vol.setProperty("unknown", unknown)
            self.vol.style().unpolish(self.vol)
            self.vol.style().polish(self.vol)
        self.vol.setEnabled(self.connected and not self.busy)
        self.vol_note.setText(
            "Press Apply to set this level." if self.vol_dirty else
            "The Arc can't report its level, and it resets when it restarts or is replugged. Move the slider, then "
            "press Apply." if unknown else
            "The Arc can't report its level. This is what you last set here.")
        sug = suggest_volume_db(self._boost())
        self._sug_shown = self.connected and sug < 0 and volume_step(sug) != self.vol.value()
        self.vol_suggest.setVisible(self._sug_shown)
        if self._sug_shown:
            self.vol_suggest.setText(f"Use suggested {sug:+.1f} dB")
            self.vol_suggest.setToolTip("Leaves headroom for this sound's boost so loud bass is less likely to distort.")

    def on_app_state(self, state):
        """When the window becomes active again, quietly re-read the device to catch outside changes."""
        if state == Qt.ApplicationActive and self.connected and not self.busy and self.device_bands is not None:
            self.ctl.read(self.current, silent=True)      # pick up changes made by other programs

    # ---------------------------------------------------------------- device actions
    def _stuck(self):
        """Watchdog fired: a device job took too long."""
        if self.busy:
            self.flash("The device isn't responding. Unplug it, plug it back in, then press ↻.", True, sticky=True)

    def start(self, job, fn, *args):
        """Start a controller job; True if it was accepted."""
        self.job = job
        if not fn(*args):
            self.job = None
            return False
        return True

    def on_busy(self, b):
        """A device job started or ended: lock/unlock controls and run the watchdog timer."""
        self.busy = b
        if b:
            self.watchdog.start(60000 if self.job == "save" else 25000)
        else:
            self.watchdog.stop()
            self.job = None
        self.refresh_chrome()

    def on_unsupported(self, info, reason):
        """The device holds settings this app can't represent: show them as unreadable (writing needs confirmation).
        """
        if not self.current or info.key != self.current.key:
            return
        self.unsup[info.key] = reason
        self.device_bands = None
        self.flash(f"Can't read this device's settings: {reason}", True, sticky=True)
        self.refresh_all()

    def on_apply_failed(self, info, message):
        """A write failed part-way: mark the device 'unknown' and re-read it."""
        self.uncertain.add(info.key)
        self.flash(f"Writing failed part-way ({message}). The device may hold a mix of old and new values. "
                   "Re-reading it now.", True, sticky=True)
        self.refresh_all()
        if self.current and self.current.key == info.key:
            QTimer.singleShot(300, lambda: self.read(silent=True))     # quiet, so this message stays visible

    def on_failed(self, message, retry):
        """A background job failed: show the message (with a Try again button for retryable errors)."""
        hint = " Check the cable, close the web editor if it's open, then press Try again." if retry else ""
        self.flash(message + hint, True, sticky=True)
        self.retry_btn.setVisible(retry and self.connected)

    def read(self, silent=False):
        """Ask the controller to read the current device (``silent`` = background refresh)."""
        if not self.current or self.ctl.cancel.is_set():
            return
        if self.busy:
            QTimer.singleShot(300, lambda: self.read(silent))     # try again once the current job ends
            return
        if not silent:
            self.retry_btn.hide()
        self.start("read", self.ctl.read, self.current, silent)

    def on_loaded(self, info, fw, bands, silent):
        """The device's bands arrived. Remember them (first read: also keep a backup) and show them, unless the user has unsent edits on screen."""
        if not self.current or info.key != self.current.key:
            return                                          # a reply for a device we've since left
        self.retry_btn.hide()
        self.uncertain.discard(info.key)
        self.unsup.pop(info.key, None)
        self.firmware = fw
        prev = self.device_bands
        replace = (prev is not None and self.bands == prev) or (prev is None and self.current_preset() is not None)
        self.device_bands = list(bands)
        if info.key not in self.original:
            self.original[info.key] = list(bands)
            try:
                save_backup(info.name, bands)
            except OSError:
                pass
        if replace:
            self.bands = list(bands)
        self.refresh_all()
        if not silent:
            self.flash("Loaded the current sound from your device." if replace
                       else "Device loaded. Your unsaved tuning is still on screen.")

    def confirm_device(self):
        """Ask once per device (per session) before sending anything to an untested device or firmware."""
        d = self.current
        if not d:
            return False
        reasons = []
        if not d.verified:
            reasons.append("This device hasn't been tested with this app.")
        elif self.firmware and d.firmwares and self.firmware not in d.firmwares:
            reasons.append(f"Firmware {self.firmware} hasn't been tested with this app.")
        if d.key in self.confirmed or not reasons:
            return True
        if self.msg("Send settings to this device?",
                    "\n".join(reasons) + "\n\nA copy of its current settings has been saved, and you can restore "
                    "it from More ▸ Restore original sound.", "Continue"):
            self.confirmed.add(d.key)
            return True
        return False

    def confirm_write(self):
        """All safety checks before any write: verified device, boost cap, strong-boost warning, unreadable settings.
        """
        if not self.confirm_device():
            return False
        boost = self._boost()
        if boost > MAX_BOOST_DB:
            self.note("Too much boost", f"This sound boosts by up to +{boost:.1f} dB in total, which is more than the "
                      f"app will send to in-ear monitors (+{MAX_BOOST_DB:.0f} dB). Lower some of the gains and try again.")
            return False
        if boost > LOUD_DB:
            safe = volume_step(suggest_volume_db(boost))
            pick = self.choose("Very strong boost",
                               f"This sound boosts some frequencies by up to +{boost:.1f} dB, which can distort or be "
                               "loud. Lower the output level before listening.",
                               [f"Apply and set level to {volume_db(safe):+.1f} dB", "Apply anyway"])
            if pick is None:
                return False
            if pick == 0 and safe != self.current_volume_step():
                self.vol_pending = safe                      # sent together with the sound
                self.sync_volume_widgets()
        if self.current.key in self.unsup and not self.msg(
                "Replace unreadable settings?",
                "This device holds settings the app can't show. Applying will overwrite them.", "Overwrite"):
            return False
        return True

    def _ready(self):
        """Common checks before sending anything. True if we may go on."""
        if not self.connected or self.busy:
            return False
        if self.device_bands is None and self.current.key not in self.unsup:
            self.flash("Still reading the device. Try again in a moment.", True)
            return False
        return True

    def apply(self):
        """Try the sound (and any staged output level) on the Arc now. Live only: nothing is saved to its memory."""
        if not self.connected or self.busy:
            return
        bands_dirty = self.dirty or self.current.key in self.unsup
        if not (bands_dirty or self.vol_dirty) or not self._ready():
            return
        if bands_dirty:
            if not self.confirm_write():
                return
            step = self.vol_pending if self.vol_dirty else None
            if step is not None and not self.confirm_level(step):
                return
            self.flash("Applying…")
            ok = self.start("apply", self.ctl.apply, self.current, list(self.bands), self.firmware or None, step)
        else:                                            # only the level changed
            step = self.vol_pending
            if not self.confirm_device() or not self.confirm_level(step):
                self.vol_pending = None
                self.sync_volume_widgets()
                return
            ok = self.start("volume", self.ctl.set_volume, self.current, step, self.firmware or None)
        if not ok:
            self.flash("Busy. Try again in a moment.", True)

    def save_to_device(self):
        """Keep the sound on the Arc after unplugging: write it, save it to the Arc's memory, wait for the restart."""
        if not self.connected or self.busy:
            return
        reason = self.save_block_reason()
        if reason:
            self.flash(reason, True)
            return
        if not self.confirm_write():
            return
        step = self.vol_pending if self.vol_pending is not None else self.current_volume_step()
        if self.vol_dirty and not self.confirm_level(step):
            return
        if not self._save_warned:
            if not self.msg("Save to your Arc?",
                            "Saving makes the Arc restart for a few seconds. Sound may briefly play from your "
                            "computer's speakers, and the Arc's output level resets (the app puts your level back "
                            "if you set one). Keep the volume low, and don't unplug it while it saves.",
                            "Save", warn=False):
                return
            self._save_warned = True
        self.flash("Saving to your Arc… it restarts for a few seconds. Don't unplug it.", sticky=True)
        if not self.start("save", self.ctl.save, self.current, list(self.bands), self.firmware or None, step):
            self.flash("Busy. Try again in a moment.", True)

    def on_applied(self, info, bands, sent):
        """Live write succeeded: the Arc plays the new sound; it is not yet saved to its memory."""
        if not self.current or info.key != self.current.key:
            return
        self.uncertain.discard(info.key)
        self.unsup.pop(info.key, None)
        self.device_bands = list(bands)
        self.unsaved.add(info.key)                          # live now; the Arc's memory still holds the old sound
        self.saved_keys.discard(info.key)
        ok = list(bands) == list(sent)
        self.refresh_all()
        self.flash("Applied. It stays on the Arc until you unplug it; press Save to Arc to keep it." if ok else
                   "Sent, but the device reported different values. Use More ▸ Restore original sound if it "
                   "sounds wrong.", not ok, sticky=not ok)

    def on_saved(self, info, bands, sent, res):
        """Save to the Arc's memory succeeded and the Arc has come back after its restart."""
        if not self.current or info.key != self.current.key:
            return
        self.current = info                                 # the Arc re-attached: it may have new paths
        self.devs = [info if d.key == info.key else d for d in self.devs]
        self.uncertain.discard(info.key)
        self.unsup.pop(info.key, None)
        self.device_bands = list(bands)
        ok = list(bands) == list(sent)
        if res["restart_seen"] and ok:
            self.unsaved.discard(info.key)
            self.saved_keys.add(info.key)
        else:
            self.unsaved.add(info.key)
        self.refresh_all()
        if not res["restart_seen"]:
            self.flash("Sent, but the Arc didn't restart, so I can't confirm it was saved. Unplug it, plug it back "
                       "in, then press ↻ to check what it holds.", True, sticky=True)
        elif not ok:
            self.flash("Saved, but the device reported different values. Use More ▸ Restore original sound if it "
                       "sounds wrong.", True, sticky=True)
        elif res["level_restored"] is not None:
            self.flash(f"Saved on your Arc. Output level put back to {volume_db(res['level_restored']):+.1f} dB.")
        elif res["level_error"]:
            self.flash("Saved, but the output level couldn't be put back. The Arc is at its default level: check it "
                       "before you listen.", True, sticky=True)
        else:
            self.flash("Saved on your Arc. The output level reset: set it before you listen.", sticky=True)

    def revert(self):
        """Go back to what is on the device right now (also drops a staged level)."""
        self.vol_pending = None
        if self.device_bands:
            self.bands = list(self.device_bands)
            self.refresh_all()
        self.sync_volume_widgets()

    def dismiss_intro(self):
        """Hide the first-run hint for good."""
        self.intro.hide()
        prefs = load_prefs()
        prefs["intro_seen"] = True
        save_prefs(prefs)

    def restore_backup(self):
        """Pick one of the automatic backups of what a device held before this app changed it."""
        found = list_backups()
        if not found:
            self.flash("No backups yet. One is saved the first time the app reads a device.", True)
            return
        if not self.confirm_discard():
            return
        labels = []
        for _, when, name, b in found:
            lab = f"{when}  ·  {name}  ·  peak boost {max(0.0, peak_boost(b)):+.1f} dB"
            while lab in labels:
                lab += " ·"
            labels.append(lab)
        self.ctl.paused = True
        try:
            label, ok = QInputDialog.getItem(self, "Restore from backup",
                                             "Choose a backup. It loads on screen; press Apply to write it.",
                                             labels, 0, False)
        finally:
            self.ctl.paused = False
        if ok and label in labels:
            self.bands = list(found[labels.index(label)][3])
            self.refresh_all()
            self.flash("Backup loaded. Press Apply to try it.")

    def restore_original(self):
        """Load the sound the Arc held when this session began (press Apply to write it back)."""
        if self.current and self.current.key in self.original:
            if self.confirm_discard():
                self.bands = list(self.original[self.current.key])
                self.refresh_all()
                self.flash("Original sound loaded. Press Apply to write it back.")
        else:
            self.flash("Connect a device first.", True)

    def closeEvent(self, e):
        """Ask before discarding a hand-tuned sound, then stop background work and close."""
        if self.unsaved_custom() and not self.confirm_discard():
            e.ignore()
            return
        self.watchdog.stop()
        self.ctl.shutdown()
        e.accept()


def main():
    """Entry point: set up logging, the Qt app, the icon, pick the real or demo backend and show the window."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="try the UI with two pretend devices")
    args, qt_args = ap.parse_known_args()
    try:
        from .core import data_dir
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(str(data_dir() / "arcpeq.log"), maxBytes=500_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)
    except Exception:                                       # noqa: BLE001  (logging is optional)
        pass
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("Arc PEQ")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(STYLE)
    app.setWindowIcon(app_icon())
    try:
        backend = DemoBackend() if args.demo else RealBackend()
        if not args.demo:
            import hid  # noqa: F401
    except ImportError:
        print("Missing dependency. Run:  pip install -r requirements.txt  (the package is 'hidapi')")
        sys.exit(1)
    win = MainWindow(backend, demo=args.demo)
    win.show()
    sys.exit(app.exec())
