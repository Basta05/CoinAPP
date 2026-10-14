"""Záložka 👁 Watchlist – GEM / SHORT / LONG watch a archiv s „Pull archiv“ (co coiny udělaly po vyhození)."""
import threading
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QListView, QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from coinscan import watch as W

from .widgets import CoinDelegate, CoinListModel, fmt_usd

COLS = (("gem", "🟢 GEM watch"), ("short", "🟡 SHORT watch"), ("long", "🟠 LONG watch"))


class ArchiveModel(QAbstractTableModel):
    COLS = ["Archivován", "Coin", "Proč vyhozen", "Ze stavu", "MC při vyhození", "Max po vyhození", "Max ×", "Teď ×"]

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def rowCount(self, p=QModelIndex()):
        return len(self.rows)

    def columnCount(self, p=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        return self.COLS[s] if role == Qt.DisplayRole and o == Qt.Horizontal else None

    def data(self, idx, role=Qt.DisplayRole):
        r = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            return [datetime.fromtimestamp(r["archived_ts"]).strftime("%d.%m %H:%M"), r.get("symbol"), r.get("reason"),
                    W.STATE_CZ.get(r.get("from_state"), r.get("from_state") or ""), fmt_usd(r.get("mcap_at")),
                    fmt_usd(r.get("peak_after")) if r.get("peak_after") else "–",
                    f"{r['mult_after']:.1f}×" if r.get("mult_after") else "–",
                    f"{r['mult_now']:.2f}×" if r.get("mult_now") else "–"][c]
        if role == Qt.ForegroundRole and c == 6 and r.get("mult_after"):
            m = r["mult_after"]
            return QColor("#ef4444" if m >= 5 else "#eab308" if m >= 2 else "#9ca3af")
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class _Sig(QObject):
    pulled = Signal(object)


class WatchView(QWidget):
    open_coin = Signal(str)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.sig = _Sig()
        self.sig.pulled.connect(self._fill_archive)
        self._seen_version = -1

        info = QLabel("Výchozí skóre se počítá zdarma pro všechny coiny. 🔴 SHIT dostane 1× Jev → dobrý jde na 🟡 SHORT, "
                      "slabý na 🟠 LONG. LONG čeká (top 200, max 15 min) – když se skóre zlepší, podívá se Jev: dobrý → SHORT, "
                      "špatný → archiv. SHORT běží v cyklech se skóre + Jevem; BUY skóre → AI report → 🟢 GEM. "
                      "Všechny prahy jsou ve Filtrech (👁 Watch stavy).")
        info.setWordWrap(True)
        info.setStyleSheet("color:#9ca3af")
        self.counts = QLabel()
        self.counts.setTextFormat(Qt.RichText)

        cols = QHBoxLayout()
        self.models, self.views, self.heads = {}, {}, {}
        for key, title in COLS:
            box = QVBoxLayout()
            h = QLabel(title)
            h.setStyleSheet("font-weight:bold; font-size:11pt; padding:4px;")
            m = CoinListModel(key)
            v = QListView()
            v.setModel(m)
            v.setItemDelegate(CoinDelegate(key, v))
            v.setUniformItemSizes(True)
            v.clicked.connect(lambda idx: self.open_coin.emit(idx.data(Qt.UserRole)["mint"]))
            box.addWidget(h)
            box.addWidget(v)
            cols.addLayout(box)
            self.models[key], self.views[key], self.heads[key] = m, v, h
        top_w = QWidget()
        top_w.setLayout(cols)

        # archiv
        arch = QWidget()
        al = QVBoxLayout(arch)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>📁 Archiv</b> – co coiny udělaly PO vyhození (bez AI):"))
        self.hours = QComboBox()
        for lab, h in (("posledních 2 h", 2), ("6 h", 6), ("24 h", 24), ("3 dny", 72)):
            self.hours.addItem(lab, h)
        self.hours.setCurrentIndex(2)
        bar.addWidget(self.hours)
        self.btn = QPushButton("📥 Pull archiv")
        self.btn.setObjectName("start")
        self.btn.setToolTip("Pro každý archivovaný coin zjistí aktuální MC a ATH po vyhození – najde chyby, "
                            "kdy jsme vyhodili coin, který pak letěl.")
        self.btn.clicked.connect(self.pull)
        bar.addWidget(self.btn)
        bar.addStretch()
        al.addLayout(bar)
        self.summary = QLabel("Klikni na Pull archiv.")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        al.addWidget(self.summary)
        self.amodel = ArchiveModel()
        self.atable = QTableView()
        self.atable.setModel(self.amodel)
        self.atable.verticalHeader().setVisible(False)
        self.atable.setSelectionBehavior(QTableView.SelectRows)
        hh = self.atable.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for i, w in enumerate((90, 90, 260, 110, 110, 120, 70)):
            self.atable.setColumnWidth(i, w)
        self.atable.doubleClicked.connect(self._open_arch)
        al.addWidget(self.atable, 1)

        split = QSplitter(Qt.Vertical)
        split.addWidget(top_w)
        split.addWidget(arch)
        split.setSizes([480, 360])
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addWidget(self.counts)
        lay.addWidget(split, 1)

        t = QTimer(self)
        t.timeout.connect(self.refresh)
        t.start(1000)

    def refresh(self):
        if not self.isVisible():
            return
        version, snap = self.store.snapshot()
        cnt = self.store.watch.counts()
        self.counts.setText(" &nbsp; ".join(f"<span style='color:{W.STATE_COL[k]}'>{W.STATE_CZ[k]}: <b>{cnt.get(k, 0)}</b></span>"
                                            for k in ("gem", "short", "long", "shit", "archived"))
                            + " &nbsp;<span style='color:#6b7280'>(archiv v paměti = poslední hodina)</span>")
        if version == self._seen_version:
            return
        self._seen_version = version
        for key, _ in COLS:
            items = [c for c in snap.values() if c.get("wstate") == key]
            items.sort(key=lambda c: -(c.get("gem") or 0))
            self.models[key].set_items(items[:200])
            self.heads[key].setText(f"{dict(COLS)[key]}  ({len(items)})")

    # ---------- pull archiv ----------
    def pull(self):
        self.btn.setEnabled(False)
        self.btn.setText("⏳ stahuji…")
        h = self.hours.currentData()

        def work():
            try:
                res = W.pull_archive(h)
                rows = [dict(r) for r in W.db.query(
                    "SELECT * FROM watch_archive WHERE archived_ts > ? ORDER BY COALESCE(mult_after,0) DESC LIMIT 500",
                    (__import__("time").time() - h * 3600,))]
                res["rows"] = rows
            except Exception as e:  # noqa: BLE001
                res = {"error": str(e)}
            self.sig.pulled.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _fill_archive(self, res):
        self.btn.setEnabled(True)
        self.btn.setText("📥 Pull archiv")
        if "error" in res:
            self.summary.setText(f"<span style='color:#ef4444'>Chyba: {res['error']}</span>")
            return
        t = res["tiers"]
        n = res["pulled_n"] or 0
        pct = lambda a: f"{a / n * 100:.0f} %" if n else "–"  # noqa: E731
        html = (f"Archivováno <b>{res['total']}</b>, zkontrolováno <b>{n}</b>. Po vyhození udělalo: "
                f"<b style='color:#eab308'>2×+ {t[2]}</b> ({pct(t[2])}) · <b style='color:#f97316'>5×+ {t[5]}</b> · "
                f"<b style='color:#ef4444'>10×+ {t[10]}</b> · <b style='color:#ef4444'>20×+ {t[20]}</b>")
        if res["by_reason"]:
            html += ("<br><table cellpadding='3'><tr style='color:#9ca3af'><td>Důvod vyhození</td><td>coinů</td>"
                     "<td>pak 2×+</td><td>5×+</td><td>10×+</td><td>nejlepší</td></tr>")
            for k, d in sorted(res["by_reason"].items(), key=lambda x: -x[1]["x2"]):
                bad = d["n"] >= 5 and d["x2"] / d["n"] > 0.15
                html += (f"<tr><td>{'⚠ ' if bad else ''}{k}</td><td>{d['n']}</td><td>{d['x2']}</td><td>{d['x5']}</td>"
                         f"<td>{d['x10']}</td><td>{d['best']:.1f}×</td></tr>")
            html += "</table><span style='color:#9ca3af'>⚠ = pravidlo často vyhazuje coiny, které pak letí – zvaž ho povolit.</span>"
        self.summary.setText(html)
        self.amodel.set_rows(res.get("rows") or [])

    def _open_arch(self, idx):
        r = self.amodel.rows[idx.row()]
        QDesktopServices.openUrl(QUrl(f"https://pump.fun/coin/{r['mint']}"))
