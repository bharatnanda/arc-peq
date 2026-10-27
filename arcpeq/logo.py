"""The app logo, drawn in code so it needs no image files (works the same when packaged)."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap


def logo_image(size: int = 256) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256.0
    p.scale(s, s)
    bg = QLinearGradient(0, 0, 0, 256)
    bg.setColorAt(0, QColor("#232a40"))
    bg.setColorAt(1, QColor("#0e1118"))
    p.setPen(Qt.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(12, 12, 232, 232), 56, 56)
    p.setPen(QPen(QColor("#3a4466"), 3))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(13.5, 13.5, 229, 229), 55, 55)
    # faint zero line
    p.setPen(QPen(QColor("#3a4152"), 2))
    p.drawLine(QPointF(44, 136), QPointF(212, 136))
    # EQ curve: low shelf lift, mid dip, presence peak
    def y(x):
        return 136 - (34 * math.exp(-((x - 70) / 34) ** 2) - 20 * math.exp(-((x - 128) / 22) ** 2)
                      + 46 * math.exp(-((x - 180) / 26) ** 2))
    path = QPainterPath(QPointF(44, y(44)))
    for x in range(45, 213):
        path.lineTo(QPointF(x, y(x)))
    fill = QPainterPath(path)
    fill.lineTo(QPointF(212, 136))
    fill.lineTo(QPointF(44, 136))
    fill.closeSubpath()
    g = QLinearGradient(0, 80, 0, 190)
    top = QColor("#7b97ff")
    top.setAlpha(110)
    g.setColorAt(0, top)
    g.setColorAt(1, QColor(123, 151, 255, 10))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawPath(fill)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#ffffff"), 9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    for x, col in ((70, "#ff8a3d"), (128, "#b36bff"), (180, "#2ed3b7")):
        p.setPen(QPen(QColor("#0e1118"), 4))
        p.setBrush(QColor(col))
        p.drawEllipse(QPointF(x, y(x)), 13, 13)
    p.end()
    return img


def app_icon() -> QIcon:
    ic = QIcon()
    for sz in (16, 32, 64, 128, 256, 512):
        ic.addPixmap(QPixmap.fromImage(logo_image(sz)))
    return ic
