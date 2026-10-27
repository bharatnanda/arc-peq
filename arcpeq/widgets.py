"""Custom Qt widgets: the draggable response graph (``CurveWidget``) and one card per band (``BandCard``)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QLinearGradient, QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from .core import (FREQ_MAX, FREQ_MIN, GAIN_LIMIT, PEAK, Q_MAX, Q_MIN, TYPE_NAMES, Band, band_db, snap_freq,
                   total_db)

VIEW_DB = GAIN_LIMIT + 3.0                     # room for the +3 dB output level above the largest band gain
BAND_COLORS = ["#ff8a3d", "#ff5d8f", "#b36bff", "#4da3ff", "#2ed3b7"]
ACCENT = "#7b97ff"
LOG_MIN, LOG_MAX = math.log10(FREQ_MIN), math.log10(FREQ_MAX)


class CurveWidget(QWidget):
    """Frequency-response plot. Drag a handle to change a band's frequency and gain;
    scroll the mouse wheel over the plot to change the selected band's Q."""
    bandEdited = Signal(int, object)
    bandSelected = Signal(int)

    def __init__(self):
        super().__init__()
        self.bands: list[Band] = []
        self.ref: list[Band] = []
        self.selected = 0
        self.level = 0.0                     # output level in dB: shifts the whole response
        self.ref_level = 0.0
        self._drag: int | None = None
        self._wheel = 0
        self.setMinimumHeight(230)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Frequency response graph")
        self.setAccessibleDescription("Use 1 to 5 to choose a band, left and right to change frequency, up and down "
                                      "to change gain, plus and minus to change width.")
        self.setToolTip("Drag a dot, or click the graph and use the arrow keys.\n"
                        "1-5 choose a band · ←/→ frequency · ↑/↓ gain · +/− width (Shift = bigger steps)")

    def set_bands(self, bands, selected, ref=None):
        """Give the graph the bands to draw, the selected band and the 'on device' ghost curve."""
        self.bands, self.selected = list(bands), selected
        self.ref = list(ref or [])
        self.update()

    def set_level(self, db, ref_db=0.0):
        """Set the output level (dB) the whole curve is shifted by, and the level currently on the device."""
        if (db, ref_db) != (self.level, self.ref_level):
            self.level, self.ref_level = db, ref_db
            self.update()

    # geometry
    def plot(self) -> QRectF:
        """The rectangle the graph itself is drawn in (margins leave room for labels)."""
        return QRectF(48, 16, self.width() - 48 - 20, self.height() - 16 - 32)

    def fx(self, f, r):
        """Frequency (Hz) to x pixel, on a log scale."""
        f = min(max(f, FREQ_MIN), FREQ_MAX)
        return r.left() + (math.log10(f) - LOG_MIN) / (LOG_MAX - LOG_MIN) * r.width()

    def gy(self, db, r):
        """Level (dB) to y pixel; values beyond the view are clamped."""
        db = max(-VIEW_DB, min(VIEW_DB, db))
        return r.center().y() - db / VIEW_DB * r.height() / 2

    def x_to_f(self, x, r):
        """x pixel back to frequency."""
        t = min(max((x - r.left()) / r.width(), 0), 1)
        return 10 ** (LOG_MIN + t * (LOG_MAX - LOG_MIN))

    def y_to_db(self, y, r):
        """y pixel back to dB."""
        return (r.center().y() - y) / (r.height() / 2) * VIEW_DB

    def handle_pos(self, i, r):
        """Where band ``i``'s dot sits (on the band's own curve, shifted by the output level)."""
        b = self.bands[i]
        return QPointF(self.fx(b.freq, r), self.gy(band_db(b, b.freq) + self.level, r))

    def nearest(self, pos, radius=36):
        """The band whose dot is within ``radius`` pixels of ``pos``, or None."""
        r = self.plot()
        best, dist = None, radius
        for i in range(len(self.bands)):
            p = self.handle_pos(i, r)
            d = math.hypot(p.x() - pos.x(), p.y() - pos.y())
            if d < dist:
                best, dist = i, d
        return best

    # painting
    def paintEvent(self, _):
        """Draw grid, band curves, 'on device' ghost, output-level line, total curve and the dots."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#141821"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 14, 14)
        r = self.plot()
        small = QFont(self.font())
        small.setPointSizeF(9)
        p.setFont(small)

        for db in (-12, -6, 0, 6, 12):
            y = self.gy(db, r)
            p.setPen(QPen(QColor("#3a4152" if db == 0 else "#232937"), 1.4 if db == 0 else 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#9aa3b8"))
            p.drawText(QRectF(0, y - 9, r.left() - 8, 18), Qt.AlignRight | Qt.AlignVCenter, f"{db:+d}" if db else "0")
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = self.fx(f, r)
            p.setPen(QPen(QColor("#1d2230"), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            label = f"{f // 1000}k" if f >= 1000 else str(f)
            p.setPen(QColor("#9aa3b8"))
            p.drawText(QRectF(x - 24, r.bottom() + 6, 48, 18), Qt.AlignCenter, label)

        freqs = [10 ** (LOG_MIN + (LOG_MAX - LOG_MIN) * k / 240) for k in range(241)]
        zero_y = self.gy(self.level, r)
        # individual bands (faint)
        for i, b in enumerate(self.bands):
            if b.gain == 0:
                continue
            path = QPainterPath()
            for k, f in enumerate(freqs):
                pt = QPointF(self.fx(f, r), self.gy(band_db(b, f) + self.level, r))
                path.moveTo(pt) if k == 0 else path.lineTo(pt)
            c = QColor(BAND_COLORS[i])
            c.setAlpha(110 if i == self.selected else 55)
            p.setPen(QPen(c, 1.2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        # what is on the device right now (for comparison)
        if self.ref and (self.ref != self.bands or self.ref_level != self.level):
            ghost = QPainterPath()
            for k, f in enumerate(freqs):
                pt = QPointF(self.fx(f, r), self.gy(total_db(self.ref, f) + self.ref_level, r))
                ghost.moveTo(pt) if k == 0 else ghost.lineTo(pt)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#9aa3b8"), 1.6, Qt.DotLine))
            p.drawPath(ghost)
            p.setPen(QColor("#9aa3b8"))
            p.drawText(QRectF(r.right() - 190, r.top() + 4, 186, 16), Qt.AlignRight, "···  currently on your device")
        if self.level:
            p.setPen(QPen(QColor("#2ed3b7"), 1.2, Qt.DashLine))
            p.drawLine(QPointF(r.left(), zero_y), QPointF(r.right(), zero_y))
            p.setPen(QColor("#2ed3b7"))
            p.drawText(QRectF(r.left() + 6, zero_y - 18 if self.level < 10 else zero_y + 2, 190, 16),
                       Qt.AlignLeft, f"output level {self.level:+.1f} dB")
        # total
        line = QPainterPath()
        for k, f in enumerate(freqs):
            pt = QPointF(self.fx(f, r), self.gy(total_db(self.bands, f) + self.level, r))
            line.moveTo(pt) if k == 0 else line.lineTo(pt)
        fill = QPainterPath(line)
        fill.lineTo(QPointF(r.right(), zero_y))
        fill.lineTo(QPointF(r.left(), zero_y))
        fill.closeSubpath()
        grad = QLinearGradient(0, r.top(), 0, r.bottom())
        top, bot = QColor(ACCENT), QColor(ACCENT)
        top.setAlpha(95)
        bot.setAlpha(95)
        mid = QColor(ACCENT)
        mid.setAlpha(8)
        grad.setColorAt(0, top)
        grad.setColorAt(0.5, mid)
        grad.setColorAt(1, bot)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPath(fill)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#ffffff"), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)

        if self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 14, 14)
        # handles
        num = QFont(self.font())
        num.setPointSizeF(8.5)
        num.setBold(True)
        for i in range(len(self.bands)):
            c = QColor(BAND_COLORS[i])
            pos = self.handle_pos(i, r)
            sel = i == self.selected
            if sel:
                glow = QColor(c)
                glow.setAlpha(60)
                p.setPen(Qt.NoPen)
                p.setBrush(glow)
                p.drawEllipse(pos, 15, 15)
            p.setPen(QPen(QColor("#ffffff") if sel else QColor("#141821"), 2))
            p.setBrush(c)
            p.drawEllipse(pos, 9 if sel else 8, 9 if sel else 8)
            p.setPen(QColor("#10131a"))
            p.setFont(num)
            p.drawText(QRectF(pos.x() - 9, pos.y() - 9, 18, 18), Qt.AlignCenter, str(i + 1))

    # interaction
    def mousePressEvent(self, e):
        """Start dragging the nearest dot and select its band."""
        i = self.nearest(e.position())
        if i is not None:
            self._drag = i
            self.bandSelected.emit(i)

    def mouseMoveEvent(self, e):
        """While dragging: turn the pointer position into a new frequency and gain (minus the level shift)."""
        r = self.plot()
        if self._drag is None:
            self.setCursor(QCursor(Qt.PointingHandCursor if self.nearest(e.position()) is not None
                                   else Qt.ArrowCursor))
            return
        b = self.bands[self._drag]
        shown = self.y_to_db(e.position().y(), r) - self.level
        raw = shown * (2 if b.type != PEAK else 1)       # a shelf's handle shows half its gain
        gain = round(max(-GAIN_LIMIT, min(GAIN_LIMIT, raw)) * 2) / 2
        freq = snap_freq(self.x_to_f(e.position().x(), r))
        self.bandEdited.emit(self._drag, self.bands[self._drag].with_(freq=freq, gain=gain))

    def mouseReleaseEvent(self, _):
        """Stop dragging."""
        self._drag = None

    def wheelEvent(self, e):
        """Mouse wheel changes the selected band's Q (one notch = 0.1)."""
        if not self.bands:
            return
        self._wheel += e.angleDelta().y()
        steps = int(self._wheel / 120)          # one notch (120) = 0.1; trackpads accumulate smoothly
        if steps:
            self._wheel -= steps * 120
            self._nudge(q=0.1 * steps)

    def _nudge(self, freq_steps=0, gain=0.0, q=0.0):
        """Change the selected band by small steps (used by keyboard and wheel)."""
        i = self.selected
        if not self.bands or not 0 <= i < len(self.bands):
            return
        b = self.bands[i]
        nb = b
        if freq_steps:
            f = b.freq
            for _ in range(abs(freq_steps)):
                f = snap_freq(f * (1.06 if freq_steps > 0 else 1 / 1.06))
                if f == b.freq:
                    f += 1 if freq_steps > 0 else -1
            nb = nb.with_(freq=int(min(max(f, FREQ_MIN), FREQ_MAX)))
        if gain:
            nb = nb.with_(gain=min(max(round((b.gain + gain) * 2) / 2, -GAIN_LIMIT), GAIN_LIMIT))
        if q:
            nb = nb.with_(q=round(min(max(b.q + q, Q_MIN), Q_MAX), 1))
        if nb != b:
            self.bandEdited.emit(i, nb)

    def keyPressEvent(self, e):
        """Keyboard control: 1-5 pick a band, arrows move it, +/- change width, Shift = bigger steps."""
        k, big = e.key(), bool(e.modifiers() & Qt.ShiftModifier)
        if Qt.Key_1 <= k <= Qt.Key_5 and (k - Qt.Key_1) < len(self.bands):
            self.bandSelected.emit(k - Qt.Key_1)
        elif k == Qt.Key_Left:
            self._nudge(freq_steps=-(4 if big else 1))
        elif k == Qt.Key_Right:
            self._nudge(freq_steps=4 if big else 1)
        elif k == Qt.Key_Up:
            self._nudge(gain=2.0 if big else 0.5)
        elif k == Qt.Key_Down:
            self._nudge(gain=-2.0 if big else -0.5)
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self._nudge(q=0.5 if big else 0.1)
        elif k == Qt.Key_Minus:
            self._nudge(q=-0.5 if big else -0.1)
        else:
            super().keyPressEvent(e)


class BandCard(QFrame):
    """One band's controls: on/off, type, gain slider, frequency box + slider, and Q."""
    changed = Signal(int, object)
    clicked = Signal(int)
    toggled = Signal(int, bool)

    def __init__(self, idx: int):
        super().__init__()
        self.idx = idx
        self.setObjectName("bandCard")
        self.band = Band(8, 1000, 1.0, 0.0)
        color = BAND_COLORS[idx]

        title = QLabel(f"<span style='color:{color}'>●</span>&nbsp; Band {idx + 1}")
        title.setObjectName("cardTitle")
        self.type = QComboBox()
        for t, name in TYPE_NAMES.items():
            self.type.addItem(name, t)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch()
        self.on = QCheckBox()
        self.on.setAccessibleName(f"Band {idx + 1} on or off")
        self.on.setToolTip("Off sets this band's gain to 0 (the Arc has no separate off switch). "
                           "Turning it on again restores your last gain.")
        self.on.toggled.connect(lambda v: self.toggled.emit(self.idx, v))
        head.addWidget(self.on)

        self.gain_label = QLabel("+0.0 dB")
        self.gain_label.setObjectName("gainLabel")
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(-int(GAIN_LIMIT * 2), int(GAIN_LIMIT * 2))

        self.freq = QSpinBox()
        self.freq.setRange(FREQ_MIN, FREQ_MAX)
        self.freq.setSuffix(" Hz")
        self.freq.setKeyboardTracking(False)
        self.freq.setAlignment(Qt.AlignRight)
        self.freq_slider = QSlider(Qt.Horizontal)
        self.freq_slider.setRange(0, 1000)

        self.q = QDoubleSpinBox()
        self.q.setRange(Q_MIN, Q_MAX)
        self.q.setSingleStep(0.1)
        self.q.setDecimals(2)
        self.q.setKeyboardTracking(False)
        self.q.setAlignment(Qt.AlignRight)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addWidget(self.type)
        lay.addWidget(self.gain_label)
        lay.addWidget(self.gain)
        row = QHBoxLayout()
        row.addWidget(self._cap("Freq"))
        row.addStretch()
        row.addWidget(self.freq)
        lay.addLayout(row)
        lay.addWidget(self.freq_slider)
        row = QHBoxLayout()
        row.addWidget(self._cap("Width"))
        row.addStretch()
        row.addWidget(self.q)
        lay.addLayout(row)

        self.type.currentIndexChanged.connect(self._on_type)
        self.gain.valueChanged.connect(self._on_gain)
        self.freq.valueChanged.connect(self._on_freq_spin)
        self.freq_slider.valueChanged.connect(self._on_freq_slider)
        self.q.valueChanged.connect(self._on_q)
        n = idx + 1
        self.type.setAccessibleName(f"Band {n} filter type")
        self.gain.setAccessibleName(f"Band {n} gain in decibels")
        self.freq.setAccessibleName(f"Band {n} frequency in hertz")
        self.freq_slider.setAccessibleName(f"Band {n} frequency slider")
        self.q.setAccessibleName(f"Band {n} Q, the width of the band")
        self.q.setToolTip("Q: how wide the band is. Lower = wider and gentler, higher = narrower and sharper")
        self.freq.setToolTip("Type a value, or use the up and down arrow keys")
        self.q.setToolTip("Higher = narrower. Type a value, or use the up and down arrow keys")

    @staticmethod
    def _cap(text):
        """A small grey caption label."""
        l = QLabel(text)
        l.setObjectName("caption")
        return l

    def mousePressEvent(self, e):
        """Clicking anywhere on the card selects the band."""
        self.clicked.emit(self.idx)
        super().mousePressEvent(e)

    def _pos_to_freq(self, pos):
        """Map the 0..1000 frequency slider position to a snapped frequency."""
        return snap_freq(10 ** (LOG_MIN + (LOG_MAX - LOG_MIN) * pos / 1000))

    def _show(self, band):
        """Refresh the gain readout (\"+2.0 dB\") from the band."""
        self.gain_label.setText(f"{band.gain:+.1f} dB")

    def _commit(self, **kw):
        """Change only the field the user touched, so values we don't show exactly aren't rounded away."""
        self.band = self.band.with_(**kw)
        self._show(self.band)
        self._sync_on()
        self.changed.emit(self.idx, self.band)

    def _sync_on(self):
        """Keep the On checkbox in step with the band's gain."""
        on = self.band.gain != 0
        self.on.blockSignals(True)
        self.on.setChecked(on)
        self.on.blockSignals(False)
        if self.property("off") != (not on):
            self.setProperty("off", not on)
            self.style().unpolish(self)
            self.style().polish(self)

    def _on_type(self, *_):
        """Filter type was changed in the drop-down."""
        self._commit(type=self.type.currentData())

    def _on_gain(self, v):
        """Gain slider moved."""
        self._commit(gain=v / 2)

    def _on_freq_spin(self, f):
        """Frequency typed into the box."""
        self._sync_freq_slider(f)
        self._commit(freq=int(f))

    def _on_freq_slider(self, pos):
        """Frequency slider moved."""
        f = self._pos_to_freq(pos)
        self.freq.blockSignals(True)
        self.freq.setValue(f)
        self.freq.blockSignals(False)
        self._commit(freq=f)

    def _on_q(self, v):
        """Q (width) changed."""
        self._commit(q=round(v, 3))

    def _sync_freq_slider(self, f):
        """Move the frequency slider to match the box without re-triggering it."""
        self.freq_slider.blockSignals(True)
        self.freq_slider.setValue(round((math.log10(min(max(f, FREQ_MIN), FREQ_MAX)) - LOG_MIN)
                                        / (LOG_MAX - LOG_MIN) * 1000))
        self.freq_slider.blockSignals(False)

    def set_band(self, band: Band, selected: bool):
        """Load a band into the card (and mark it selected) without emitting edit signals."""
        self.band = band
        for w in (self.type, self.gain, self.freq, self.freq_slider, self.q):
            w.blockSignals(True)
        self.type.setCurrentIndex(self.type.findData(band.type))
        self.gain.setValue(round(min(max(band.gain, -GAIN_LIMIT), GAIN_LIMIT) * 2))
        self.freq.setValue(int(min(max(band.freq, FREQ_MIN), FREQ_MAX)))
        self._sync_freq_slider(band.freq)
        self.q.setValue(min(max(band.q, Q_MIN), Q_MAX))
        for w in (self.type, self.gain, self.freq, self.freq_slider, self.q):
            w.blockSignals(False)
        self._show(band)
        self._sync_on()
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
