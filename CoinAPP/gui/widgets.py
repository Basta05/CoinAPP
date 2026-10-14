"""Sdílené widgety: formulář z configu, graf mcap, karty coinů."""
import time

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLineEdit, QSpinBox,
    QStyle, QStyledItemDelegate, QWidget,
)

RISK_COL = {"LOW": "#22c55e", "MEDIUM": "#eab308", "HIGH": "#ef4444", "?": "#6b7280"}


def fmt_usd(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}k"
    return f"${v:.0f}"


def fmt_age(minutes) -> str:
    if minutes is None:
        return "?"
    if minutes < 1:
        return f"{minutes * 60:.0f}s"
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


# ---------------------------------------------------------------- formulář
def get_path(d: dict, path: str, default=None):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def set_path(d: dict, path: str, value):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


class ConfigForm(QWidget):
    """Postaví formulář ze schématu: (sekce, [(cesta, typ, param, popisek, tooltip)])."""
    changed = Signal()

    def __init__(self, schema, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout
        self.fields: dict[str, tuple[str, QWidget]] = {}
        grid = QGridLayout(self)
        for i, (title, rows) in enumerate(schema):
            box = QGroupBox(title)
            form = QFormLayout(box)
            for path, kind, param, label, tip in rows:
                w = self._make(kind, param)
                w.setToolTip(tip or "")
                form.addRow(label, w)
                self.fields[path] = (kind, w)
            grid.addWidget(box, i // 2, i % 2)

    def _make(self, kind, param):
        if kind == "int":
            w = QSpinBox()
            w.setRange(*param)
            w.setSingleStep(max(1, (param[1] - param[0]) // 200))
            w.valueChanged.connect(self.changed)
        elif kind == "float":
            w = QDoubleSpinBox()
            lo, hi, step = param
            w.setRange(lo, hi)
            w.setSingleStep(step)
            w.setDecimals(2 if step < 1 else 1)
            w.valueChanged.connect(self.changed)
        elif kind == "bool":
            w = QCheckBox()
            w.toggled.connect(self.changed)
        elif kind == "choice":
            w = QComboBox()
            w.addItems(param)
            w.currentTextChanged.connect(self.changed)
        elif kind == "multi":
            w = QWidget()
            from PySide6.QtWidgets import QHBoxLayout
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            w.boxes = {}
            for key in param:
                cb = QCheckBox(key)
                cb.toggled.connect(self.changed)
                lay.addWidget(cb)
                w.boxes[key] = cb
        else:
            w = QLineEdit()
            w.textChanged.connect(self.changed)
        return w

    def load(self, cfg: dict):
        for path, (kind, w) in self.fields.items():
            v = get_path(cfg, path)
            w.blockSignals(True)
            if kind == "int":
                w.setValue(int(v or 0))
            elif kind == "float":
                w.setValue(float(v or 0))
            elif kind == "bool":
                w.setChecked(bool(v))
            elif kind == "choice":
                w.setCurrentText(str(v))
            elif kind == "multi":
                for key, cb in w.boxes.items():
                    cb.setChecked(key in (v or []))
            elif kind == "list":
                w.setText(", ".join(v or []))
            else:
                w.setText("" if v is None else str(v))
            w.blockSignals(False)

    def dump_into(self, cfg: dict):
        for path, (kind, w) in self.fields.items():
            if kind == "int":
                v = w.value()
            elif kind == "float":
                v = round(w.value(), 4)
            elif kind == "bool":
                v = w.isChecked()
            elif kind == "choice":
                v = w.currentText()
            elif kind == "multi":
                v = [k for k, cb in w.boxes.items() if cb.isChecked()]
            elif kind == "list":
                v = [x.strip() for x in w.text().split(",") if x.strip()]
            else:
                v = w.text()
            set_path(cfg, path, v)


# ---------------------------------------------------------------- graf
class Sparkline(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[tuple[float, float]] = []
        self.setMinimumHeight(110)

    def set_points(self, pts):
        self.points = pts
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(4, 14, -4, -4)
        p.fillRect(self.rect(), QColor("#0b0d12"))
        p.setPen(QColor("#6b7280"))
        p.setFont(QFont("Segoe UI", 8))
        if len(self.points) < 2:
            p.drawText(self.rect(), Qt.AlignCenter, "sbírám data o mcap…")
            return
        xs = [t for t, _ in self.points]
        ys = [v for _, v in self.points]
        x0, x1 = min(xs), max(xs) or 1
        y0, y1 = min(ys), max(ys)
        if y1 == y0:
            y1 = y0 + 1
        path = QPainterPath()
        for i, (t, v) in enumerate(self.points):
            x = r.left() + (t - x0) / max(x1 - x0, 1) * r.width()
            y = r.bottom() - (v - y0) / (y1 - y0) * r.height()
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        up = ys[-1] >= ys[0]
        p.setPen(QPen(QColor("#22c55e" if up else "#ef4444"), 2))
        p.drawPath(path)
        chg = (ys[-1] / ys[0] - 1) * 100 if ys[0] else 0
        p.setPen(QColor("#9ca3af"))
        p.drawText(6, 11, f"MCap {fmt_usd(ys[-1])}  ({chg:+.1f} % za {fmt_age((x1 - x0) / 60)})"
                          f"   max {fmt_usd(max(ys))}  min {fmt_usd(min(ys))}")


# ---------------------------------------------------------------- karty coinů
class CoinListModel(QAbstractListModel):
    def __init__(self, column: str):
        super().__init__()
        self.column = column
        self.items: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        c = self.items[index.row()]
        if role == Qt.UserRole:
            return c
        if role == Qt.DisplayRole:
            return c["symbol"]
        if role == Qt.ToolTipRole:
            return f"{c['name']}\n{c['mint']}"
        return None

    def set_items(self, items: list[dict]):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def row_of(self, mint: str) -> int:
        for i, c in enumerate(self.items):
            if c["mint"] == mint:
                return i
        return -1


class CoinDelegate(QStyledItemDelegate):
    H = 66

    def __init__(self, column: str, parent=None):
        super().__init__(parent)
        self.column = column

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.H)

    def paint(self, p: QPainter, option, index):
        c = index.data(Qt.UserRole)
        if not c:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(3, 2, -3, -2)
        sel = option.state & QStyle.State_Selected
        hov = option.state & QStyle.State_MouseOver
        _wc = {"shit": "#7f1d1d", "long": "#9a3412", "short": "#854d0e", "gem": "#166534"}.get(c.get("wstate"), "#1f2937")
        p.setPen(QPen(QColor("#3b82f6" if sel else _wc), 1.4))
        is_bot = (c.get("pat") or {}).get("verdict") == "bot"
        bg = "#1e293b" if sel else "#161b26" if hov else "#11151e"
        if is_bot and not sel:
            bg = "#2a1215"
        p.setBrush(QBrush(QColor(bg)))
        p.drawRoundedRect(r, 6, 6)

        # riziko – barevný proužek vlevo
        rl = c.get("risk_label") or "?"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(RISK_COL.get(rl, "#6b7280")))
        p.drawRoundedRect(QRectF(r.left(), r.top(), 4, r.height()), 2, 2)

        x = r.left() + 12
        # řádek 1: symbol, jméno, stáří
        p.setPen(QColor("#f3f4f6"))
        f = QFont("Segoe UI", 10, QFont.Bold)
        p.setFont(f)
        sym = c["symbol"][:14]
        p.drawText(QRectF(x, r.top() + 4, 150, 18), Qt.AlignLeft | Qt.AlignVCenter, sym)
        sym_w = p.fontMetrics().horizontalAdvance(sym)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor("#9ca3af"))
        name_w = r.width() - sym_w - 175
        p.drawText(QRectF(x + sym_w + 6, r.top() + 4, max(10, name_w), 18), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(c["name"], Qt.ElideRight, int(max(10, name_w))))
        gem = c.get("gem")
        ai = c.get("ai") or {}
        ws = c.get("wstate")
        if ws:
            wcol = {"shit": "#ef4444", "long": "#f97316", "short": "#eab308", "gem": "#22c55e", "archived": "#6b7280"}[ws]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(wcol))
            p.drawRoundedRect(QRectF(r.right() - 10, r.top() + 4, 6, 6), 3, 3)
            p.setFont(QFont("Segoe UI", 7, QFont.Bold))
            p.setPen(QColor(wcol))
            p.drawText(QRectF(r.right() - 64, r.bottom() - 26, 58, 12), Qt.AlignRight | Qt.AlignVCenter,
                       {"shit": "SHIT", "long": "LONG", "short": "SHORT", "gem": "GEM", "archived": "ARCH"}[ws])
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QColor("#9ca3af"))
        jev = (c.get("jev") or {}).get("score")
        if (gem is not None and gem >= 40) or jev is not None:
            p.setFont(QFont("Segoe UI", 8, QFont.Bold))
            p.setPen(QColor("#c084fc" if (gem or 0) >= 60 else "#8b5cf6"))
            tag = (f"💎{gem}" if gem is not None else "") + (f" J{jev}" if jev is not None else "") \
                + (f" {ai['verdict']}" if ai.get("verdict") else "")
            p.drawText(QRectF(r.right() - 160, r.top() + 4, 80, 18), Qt.AlignRight | Qt.AlignVCenter, tag)
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QColor("#9ca3af"))
        if self.column == "migrated" and c.get("migrated_at"):
            age = f"migr. {fmt_age((time.time() - c['migrated_at']) / 60)}"
        else:
            age = fmt_age(c["age_min"])
        p.drawText(QRectF(r.right() - 90, r.top() + 4, 84, 18), Qt.AlignRight | Qt.AlignVCenter, age)

        # řádek 2: MC, ATH, badge
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.setPen(QColor("#60a5fa"))
        p.drawText(QRectF(x, r.top() + 23, 90, 16), Qt.AlignLeft | Qt.AlignVCenter, f"MC {fmt_usd(c['mcap'])}")
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor("#9ca3af"))
        badges = [f"ATH {fmt_usd(c['ath_mcap'])}"]
        if c["replies"]:
            badges.append(f"💬{c['replies']}")
        soc = "".join(s for s, k in (("𝕏", "twitter"), (" TG", "telegram"), (" W", "website")) if c.get(k))
        if soc:
            badges.append(soc.strip())
        if c.get("is_live"):
            badges.append("🔴LIVE")
        p.drawText(QRectF(x + 92, r.top() + 23, r.width() - 92 - 12 - 95, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText("  ·  ".join(badges), Qt.ElideRight, int(r.width() - 92 - 12 - 95)))
        # risk badge vpravo (bot má přednost)
        if is_bot:
            p.setPen(QColor("#f87171"))
            p.setFont(QFont("Segoe UI", 8, QFont.Bold))
            p.drawText(QRectF(r.right() - 90, r.top() + 23, 84, 16), Qt.AlignRight | Qt.AlignVCenter,
                       f"🤖 BOT {c['pat'].get('score', '')}")
        elif rl != "?":
            p.setPen(QColor(RISK_COL[rl]))
            p.setFont(QFont("Segoe UI", 8, QFont.Bold))
            p.drawText(QRectF(r.right() - 90, r.top() + 23, 84, 16), Qt.AlignRight | Qt.AlignVCenter,
                       f"{rl} {c.get('risk_score') or ''}")

        # řádek 3: progress bar bonding curve
        bar = QRectF(x, r.bottom() - 13, r.width() - 12 - 62, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1f2937"))
        p.drawRoundedRect(bar, 3, 3)
        prog = c["progress"] / 100
        col = "#a855f7" if c["complete"] else "#22c55e" if prog >= 0.8 else "#eab308" if prog >= 0.5 else "#3b82f6"
        p.setBrush(QColor(col))
        p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * prog, bar.height()), 3, 3)
        p.setPen(QColor("#d1d5db"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(bar.right() + 4, bar.top() - 5, r.right() - bar.right() - 10, 16), Qt.AlignRight | Qt.AlignVCenter,
                   "MIGR." if c["complete"] else f"{c['progress']:.0f} %")
        p.restore()
