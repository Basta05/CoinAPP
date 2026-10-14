"""Záložka Coiny (DB) – co scanner našel, proč coin prošel/neprošel a jak dopadl. Lidskou řečí."""
import json
import threading
import time
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QSplitter, QTableView, QTextBrowser, QVBoxLayout, QWidget,
)

from coinscan import db
from coinscan.stats import STAGE_CZ

from .widgets import fmt_usd

OUTCOME = {
    "pending": ("⏳ sleduje se", "#60a5fa"),
    "hit": ("🚀 cíl splněn", "#22c55e"),
    "miss": ("😐 nic se nestalo", "#9ca3af"),
    "rug": ("💀 rug / propad", "#ef4444"),
    "untracked": ("– nesleduje se", "#6b7280"),
}
RISK_BG = {"LOW": "#14532d", "MEDIUM": "#713f12", "HIGH": "#7f1d1d"}
TIPS = [
    "Kdy scanner coin našel",
    "Symbol coinu",
    "Kam se coin v pipeline dostal:\nzákladní filtr → bot filtr → RugCheck → AI → prošel",
    "Proč neprošel (nebo verdikt AI)",
    "Risk rating 0–100 (LOW/MEDIUM/HIGH)",
    "Market cap ve chvíli, kdy ho scanner našel",
    "Nejvyšší násobek od nálezu (max MC ÷ MC při nálezu)",
    "Násobek teď",
    "Jak coin dopadl během 24 h sledování",
]


class CoinsModel(QAbstractTableModel):
    COLS = ["Nalezen", "Coin", "Stav", "Důvod / AI", "Risk", "MC při nálezu", "Max ×", "Teď ×", "Výsledek"]

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def rowCount(self, p=QModelIndex()):
        return len(self.rows)

    def columnCount(self, p=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.COLS[s]
            if role == Qt.ToolTipRole:
                return TIPS[s]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        r = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.UserRole + 1:     # řazení
            return [r["first_seen"], r.get("symbol") or "", r["stage"], r.get("reject_reason") or "",
                    r.get("risk_score") or 0, r.get("mcap0") or 0, r.get("peak_mult") or 0,
                    r.get("last_mult") or 0, r.get("outcome") or ""][c]
        if role == Qt.DisplayRole:
            reason = r.get("reject_reason") or ""
            if r["stage"] == "passed" and r.get("llm_verdict"):
                reason = f"AI {r['llm_verdict']} {(r.get('llm_confidence') or 0) * 100:.0f} % – {r.get('llm_summary') or ''}"
            left = ""
            if r.get("outcome") == "pending" and r.get("track_until"):
                h = max(0, (r["track_until"] - time.time()) / 3600)
                left = f" ({h:.0f} h)"
            return [
                datetime.fromtimestamp(r["first_seen"]).strftime("%d.%m %H:%M"),
                r.get("symbol") or "?",
                STAGE_CZ.get(r["stage"], r["stage"]),
                reason[:90],
                f"{r['risk_label']} {r.get('risk_score') or ''}" if r.get("risk_label") else "",
                fmt_usd(r.get("mcap0")),
                f"{(r.get('peak_mult') or 1):.2f}×",
                f"{(r.get('last_mult') or 1):.2f}×",
                OUTCOME.get(r.get("outcome"), ("?", ""))[0] + left,
            ][c]
        if role == Qt.ForegroundRole:
            if c == 8:
                return QColor(OUTCOME.get(r.get("outcome"), ("", "#e5e7eb"))[1])
            if c == 6 and (r.get("peak_mult") or 0) >= 2:
                return QColor("#22c55e")
            if c == 2 and r["stage"] == "passed":
                return QColor("#22c55e")
        if role == Qt.BackgroundRole and c == 4 and r.get("risk_label") in RISK_BG:
            return QColor(RISK_BG[r["risk_label"]])
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class _Sig(QObject):
    loaded = Signal(object)


def explain(r: dict) -> str:
    """Vysvětlení jednoho coinu lidskou řečí."""
    flags = json.loads(r["llm_flags"]) if r.get("llm_flags") else []
    risks = json.loads(r["rug_risks"]) if r.get("rug_risks") else []
    st = r["stage"]
    o_txt, o_col = OUTCOME.get(r.get("outcome"), ("?", "#e5e7eb"))
    steps = {
        "rejected_basic": "❌ Zastavil se hned na <b>základním filtru</b> (objem, likvidita, MC, socials…). "
                          "Na RugCheck ani AI se vůbec nedostal.",
        "rejected_pattern": "🤖 <b>Bot filtr</b> v grafu našel fake vzor (schody / pila / stejné objemy / wash trading).",
        "rejected_safety": "🛡 Prošel základním filtrem, ale <b>RugCheck</b> našel problém.",
        "rejected_llm": "🤖 Prošel filtry i RugCheckem, ale <b>AI</b> ho nedoporučila.",
        "llm_error": "⚠ Prošel filtry, ale AI analýza selhala.",
        "passed": "✅ Prošel <b>všemi</b> kroky – tohle je tvůj signál.",
    }
    pm = r.get("peak_mult") or 1
    if r.get("outcome") == "pending":
        res = f"Pořád se sleduje. Zatím nejvýš <b>{pm:.2f}×</b> od nálezu, teď {(r.get('last_mult') or 1):.2f}×."
    elif r.get("outcome") == "hit":
        res = f"Od nálezu vyletěl až na <b>{pm:.2f}×</b> – cíl splněn."
    elif r.get("outcome") == "rug":
        res = f"Propadl se (rug). Maximum bylo {pm:.2f}×."
    else:
        res = f"Za 24 h nedosáhl cíle (maximum {pm:.2f}×)."
    lesson = ""
    if st != "passed" and pm >= 2:
        lesson = ("<p style='color:#eab308'>💡 Tenhle coin filtr vyhodil, ale pak udělal "
                  f"{pm:.1f}×. Když se to u stejného důvodu opakuje, zvaž ten filtr povolit.</p>")
    if st == "passed" and r.get("outcome") == "rug":
        lesson = "<p style='color:#eab308'>💡 Prošel vším a stejně rugnul – koukni, co filtry minuly.</p>"
    html = (f"<h3>{r.get('symbol')} <span style='color:#9ca3af'>{r.get('name') or ''}</span></h3>"
            f"<p>{steps.get(st, st)}<br>"
            + (f"<b>Důvod:</b> {r['reject_reason']}<br>" if r.get("reject_reason") else "")
            + f"<b>Výsledek:</b> <span style='color:{o_col}'>{o_txt}</span> – {res}</p>{lesson}"
            f"<p><b>Při nálezu:</b> MC {fmt_usd(r.get('mcap0'))}, likvidita {fmt_usd(r.get('liq0'))}, "
            f"objem 1h {fmt_usd(r.get('vol_h1_0'))}, stáří {(r.get('age_min0') or 0):.0f} min</p>")
    if r.get("llm_verdict"):
        html += (f"<p><b>AI:</b> {r['llm_verdict']} ({(r.get('llm_confidence') or 0) * 100:.0f} %) – "
                 f"{r.get('llm_summary') or ''}<br><b>Narativ:</b> {r.get('llm_narrative') or '—'}<br>"
                 f"<b>Red flags:</b> {', '.join(flags) or '—'}</p>")
    if r.get("rug_score") is not None:
        html += (f"<p><b>RugCheck:</b> skóre {r['rug_score']}, mint {'⚠' if r.get('mint_auth') else 'ne'}, "
                 f"freeze {'⚠' if r.get('freeze_auth') else 'ne'}, top10 {r.get('top10_pct')} %"
                 + "".join(f"<br>• [{x['level']}] {x['name']}" for x in risks[:8]) + "</p>")
    html += (f"<p><a style='color:#60a5fa' href='https://pump.fun/coin/{r['address']}'>pump.fun</a> · "
             f"<a style='color:#60a5fa' href='https://dexscreener.com/solana/{r['address']}'>DexScreener</a> · "
             f"<a style='color:#60a5fa' href='https://rugcheck.xyz/tokens/{r['address']}'>RugCheck</a><br>"
             f"<span style='color:#6b7280'>{r['address']}</span></p>")
    return html


class CoinsView(QWidget):
    def __init__(self):
        super().__init__()
        self.sig = _Sig()
        self.sig.loaded.connect(self._fill)
        self._busy = False
        self._again = False
        help_ = QLabel("Každý coin, který <b>scanner</b> našel. Cesta coinu: základní filtr → bot filtr → RugCheck → AI. "
                       "Zamítnuté se sledují taky – tak poznáš, jestli filtry nevyhazují dobré coiny. "
                       "Klikni na řádek pro vysvětlení, dvojklik = DexScreener. Najetím na hlavičku uvidíš, co sloupec znamená.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color:#9ca3af")
        self.stage = QComboBox()
        self.stage.addItem("Vše", None)
        for k, v in STAGE_CZ.items():
            self.stage.addItem(v, k)
        self.stage.currentIndexChanged.connect(self.refresh)
        self.count = QLabel()
        top = QHBoxLayout()
        top.addWidget(QLabel("Zobrazit:"))
        top.addWidget(self.stage)
        top.addStretch()
        top.addWidget(self.count)

        self.model = CoinsModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole + 1)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for i, w in enumerate((90, 90, 190, 330, 90, 95, 65, 65)):
            self.table.setColumnWidth(i, w)
        self.table.clicked.connect(self._show)
        self.table.doubleClicked.connect(self._open)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([520, 260])
        lay = QVBoxLayout(self)
        lay.addWidget(help_)
        lay.addLayout(top)
        lay.addWidget(split, 1)

    def refresh(self):
        if self._busy:
            self._again = True
            return
        self._busy = True
        stage = self.stage.currentData()

        def work():
            try:
                rows = [dict(r) for r in db.list_coins(limit=800, stage=stage)]
            except Exception:  # noqa: BLE001
                rows = None
            self.sig.loaded.emit(rows)

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, rows):
        self._busy = False
        if rows is not None:
            sel = self._current()
            self.model.set_rows(rows)
            self.count.setText(f"{len(rows)} coinů")
            if sel:
                for i, r in enumerate(rows):
                    if r["address"] == sel["address"]:
                        self.table.selectRow(self.proxy.mapFromSource(self.model.index(i, 0)).row())
                        break
        if self._again:
            self._again = False
            self.refresh()

    def _row(self, idx):
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        return self.model.rows[src.row()] if 0 <= src.row() < len(self.model.rows) else None

    def _current(self):
        return self._row(self.table.currentIndex())

    def _show(self, idx):
        r = self._row(idx)
        if r:
            self.detail.setHtml(explain(r))

    def _open(self, idx):
        r = self._row(idx)
        if r:
            QDesktopServices.openUrl(QUrl(r.get("url") or f"https://dexscreener.com/solana/{r['address']}"))
