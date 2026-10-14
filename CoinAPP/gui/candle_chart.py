"""Candlestick graf (MCap) – kolečko = zoom, tažení = posun, dvojklik = zpět na živý konec."""
import time
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

UP, DOWN = QColor("#22c55e"), QColor("#ef4444")
GRID, TEXT, BG = QColor("#1b2130"), QColor("#8b93a3"), QColor("#0b0d12")


def _fmt(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


class CandleChart(QWidget):
    AXIS_W, TIME_H = 58, 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.candles: list[list[float]] = []
        self.visible = 90          # kolik svíček je vidět
        self.offset = 0            # o kolik svíček je pohled posunut od konce (0 = živě)
        self.message = "Klikni na coin"
        self.tf_sec = 15
        self.mouse: QPointF | None = None
        self._drag_x: float | None = None
        self._drag_off = 0
        self.setMouseTracking(True)
        self.setMinimumHeight(240)

    def set_data(self, candles, tf_sec: int, message: str = ""):
        if tf_sec != self.tf_sec:
            self.offset = 0
        self.candles, self.tf_sec, self.message = candles, tf_sec, message
        self.update()

    # ---------- ovládání ----------
    def wheelEvent(self, e):
        step = 1.15 if e.angleDelta().y() < 0 else 1 / 1.15
        self.visible = int(max(15, min(600, self.visible * step)))
        self.update()

    def mousePressEvent(self, e):
        self._drag_x, self._drag_off = e.position().x(), self.offset

    def mouseReleaseEvent(self, e):
        self._drag_x = None

    def mouseDoubleClickEvent(self, e):
        self.offset = 0
        self.update()

    def mouseMoveEvent(self, e):
        self.mouse = e.position()
        if self._drag_x is not None:
            cw = max(1.0, (self.width() - self.AXIS_W) / self.visible)
            shift = int((e.position().x() - self._drag_x) / cw)
            self.offset = max(0, min(len(self.candles) - 10, self._drag_off + shift))
        self.update()

    def leaveEvent(self, e):
        self.mouse = None
        self.update()

    # ---------- kreslení ----------
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        p.setFont(QFont("Segoe UI", 8))
        if not self.candles:
            p.setPen(TEXT)
            p.drawText(self.rect(), Qt.AlignCenter, self.message or "žádná data")
            return
        end = len(self.candles) - self.offset
        data = self.candles[max(0, end - self.visible):end]
        if not data:
            return
        W, H = self.width() - self.AXIS_W, self.height() - self.TIME_H
        vol_h = H * 0.18
        price_h = H - vol_h - 6
        hi = max(c[2] for c in data)
        lo = min(c[3] for c in data)
        pad = (hi - lo) * 0.08 or hi * 0.02 or 1
        hi, lo = hi + pad, max(0.0, lo - pad)
        vmax = max((c[5] for c in data), default=1) or 1
        cw = W / self.visible
        body = max(1.0, cw * 0.7)

        def y(v):
            return 4 + (hi - v) / (hi - lo) * (price_h - 4)

        # mřížka + osa Y
        p.setPen(QPen(GRID, 1))
        for i in range(5):
            v = lo + (hi - lo) * i / 4
            yy = y(v)
            p.drawLine(0, int(yy), W, int(yy))
            p.setPen(TEXT)
            p.drawText(QRectF(W + 4, yy - 8, self.AXIS_W - 6, 16), Qt.AlignLeft | Qt.AlignVCenter, _fmt(v))
            p.setPen(QPen(GRID, 1))

        # svíčky + objem
        x0 = W - len(data) * cw
        for i, (t, o, h, l, c, v) in enumerate(data):
            cx = x0 + i * cw + cw / 2
            col = UP if c >= o else DOWN
            p.setPen(QPen(col, 1))
            p.drawLine(QPointF(cx, y(h)), QPointF(cx, y(l)))
            top, bot = y(max(o, c)), y(min(o, c))
            p.fillRect(QRectF(cx - body / 2, top, body, max(1.0, bot - top)), col)
            if v:
                vh = v / vmax * vol_h
                vc = QColor(col)
                vc.setAlpha(90)
                p.fillRect(QRectF(cx - body / 2, H - vh, body, vh), vc)

        # poslední cena
        last = data[-1][4]
        ly = y(last)
        col = UP if data[-1][4] >= data[-1][1] else DOWN
        p.setPen(QPen(col, 1, Qt.DashLine))
        p.drawLine(0, int(ly), W, int(ly))
        p.fillRect(QRectF(W, ly - 8, self.AXIS_W, 16), col)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(W + 4, ly - 8, self.AXIS_W - 6, 16), Qt.AlignLeft | Qt.AlignVCenter, _fmt(last))

        # časová osa
        p.setPen(TEXT)
        fmt = "%H:%M:%S" if self.tf_sec < 60 else "%H:%M" if self.tf_sec < 3600 else "%d.%m %H:%M"
        n_labels = max(2, int(W / 90))
        for k in range(n_labels):
            i = int(k * (len(data) - 1) / max(1, n_labels - 1))
            cx = x0 + i * cw + cw / 2
            p.drawText(QRectF(cx - 40, H + 2, 80, 16), Qt.AlignCenter,
                       datetime.fromtimestamp(data[i][0]).strftime(fmt))

        # crosshair + OHLC
        info = data[-1]
        if self.mouse and self.mouse.x() < W:
            i = int((self.mouse.x() - x0) // cw)
            if 0 <= i < len(data):
                info = data[i]
                p.setPen(QPen(QColor("#4b5563"), 1, Qt.DotLine))
                cx = x0 + i * cw + cw / 2
                p.drawLine(int(cx), 0, int(cx), H)
                p.drawLine(0, int(self.mouse.y()), W, int(self.mouse.y()))
        t, o, h, l, c, v = info
        chg = (c / o - 1) * 100 if o else 0
        p.setPen(QColor("#d1d5db"))
        p.drawText(6, 14, f"{datetime.fromtimestamp(t).strftime('%H:%M:%S')}  O {_fmt(o)}  H {_fmt(h)}  "
                          f"L {_fmt(l)}  C {_fmt(c)}  ({chg:+.1f} %)  Vol ${_fmt(v)}")
        if self.offset:
            p.setPen(QColor("#eab308"))
            p.drawText(QRectF(0, 18, W - 6, 14), Qt.AlignRight, "◀ historie – dvojklik = živě")
        elif self.message:
            p.setPen(TEXT)
            p.drawText(QRectF(0, 18, W - 6, 14), Qt.AlignRight, self.message)
