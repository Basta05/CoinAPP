"""Záložka 🏁 Runneři – velké coiny, jestli jsme je chytili, AI rozbor a doporučení úprav jedním klikem."""
import json
import threading
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QSplitter, QTableView, QTextBrowser,
    QVBoxLayout, QWidget,
)

from coinscan import db
from coinscan.runners import STATUS

from .widgets import fmt_usd


class RunnerModel(QAbstractTableModel):
    COLS = ["Nalezen", "Coin", "MC teď", "ATH", "Stav u nás", "Viděli jsme při MC", "Max gem skóre", "Šlo vydělat", "AI: proč"]

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def rowCount(self, p=QModelIndex()):
        return len(self.rows)

    def columnCount(self, p=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and o == Qt.Horizontal:
            return self.COLS[s]
        if role == Qt.ToolTipRole and o == Qt.Horizontal and s == 7:
            return "ATH ÷ MC, když jsme coin poprvé viděli = kolik × šlo teoreticky udělat"
        return None

    def data(self, idx, role=Qt.DisplayRole):
        r = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            return [
                datetime.fromtimestamp(r["detected_ts"]).strftime("%d.%m %H:%M"),
                r.get("symbol"),
                fmt_usd(r.get("mcap_now")),
                fmt_usd(r.get("ath_mcap")),
                STATUS.get(r["status"], (r["status"], ""))[0],
                fmt_usd(r.get("first_mcap")) if r.get("first_mcap") else "–",
                "" if r.get("max_score") is None else r["max_score"],
                f"{r['potential_mult']:.1f}×" if r.get("potential_mult") else "–",
                (r.get("ai_why") or "")[:120],
            ][c]
        if role == Qt.ForegroundRole and c == 4:
            return QColor(STATUS.get(r["status"], ("", "#e5e7eb"))[1])
        if role == Qt.ForegroundRole and c == 7 and (r.get("potential_mult") or 0) >= 5:
            return QColor("#22c55e")
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class _Sig(QObject):
    loaded = Signal(object, object, object)
    applied = Signal(str)


class RunnersView(QWidget):
    scan_now = Signal()
    cfg_changed = Signal()

    def __init__(self):
        super().__init__()
        self.sig = _Sig()
        self.sig.loaded.connect(self._fill)
        self.sig.applied.connect(self._applied)
        self._busy = False
        self.report_id = None

        info = QLabel("Každých 10 min appka najde coiny, které za posledních 24 h vyletěly (MC nebo ATH nad $300k), "
                      "a zkontroluje, jestli je 💎 gem detektor chytil. Když ne, zjistí proč (bot filtr, filtr sloupce, "
                      "nízké skóre, AI SKIP, vůbec neviděn) a AI navrhne úpravy nastavení, aby je chytal častěji.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#9ca3af")
        self.stats = QLabel()
        self.stats.setTextFormat(Qt.RichText)
        self.btn = QPushButton("🔍 Skenovat teď")
        self.btn.clicked.connect(self._scan)
        top = QHBoxLayout()
        top.addWidget(self.stats, 1)
        top.addWidget(self.btn)

        self.model = RunnerModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for i, w in enumerate((90, 100, 80, 80, 230, 120, 100, 90)):
            self.table.setColumnWidth(i, w)
        self.table.clicked.connect(self._show)
        self.table.doubleClicked.connect(self._open)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)

        # doporučení
        self.rec_box = QVBoxLayout()
        rec_w = QWidget()
        rec_w.setLayout(self.rec_box)
        rec_scroll = QScrollArea()
        rec_scroll.setWidgetResizable(True)
        rec_scroll.setWidget(rec_w)

        left = QSplitter(Qt.Vertical)
        left.addWidget(self.table)
        left.addWidget(self.detail)
        left.setSizes([450, 250])
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(rec_scroll)
        split.setSizes([950, 520])

        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(top)
        lay.addWidget(split, 1)

    # ---------- data ----------
    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                rows = [dict(r) for r in db.query("SELECT * FROM runners ORDER BY detected_ts DESC LIMIT 300")]
                rep = db.query("SELECT * FROM runner_reports ORDER BY ts DESC LIMIT 1")
                rep = dict(rep[0]) if rep else None
                agg = [dict(r) for r in db.query("SELECT status, COUNT(*) n FROM runners GROUP BY status")]
            except Exception:  # noqa: BLE001
                rows, rep, agg = [], None, []
            self.sig.loaded.emit(rows, rep, agg)

        threading.Thread(target=work, daemon=True).start()

    def set_scanning(self, on: bool):
        self.btn.setEnabled(not on)
        self.btn.setText("⏳ Skenuji…" if on else "🔍 Skenovat teď")

    def _scan(self):
        self.set_scanning(True)
        self.scan_now.emit()

    def _fill(self, rows, rep, agg):
        self._busy = False
        self.model.set_rows(rows)
        total = sum(a["n"] for a in agg)
        by = {a["status"]: a["n"] for a in agg}
        caught = by.get("caught_buy", 0) + by.get("caught_watch", 0)
        seen_missed = total - caught - by.get("not_seen", 0)
        pct = f"{caught / total * 100:.0f} %" if total else "–"
        self.stats.setText(
            f"<b>{total}</b> runnerů · <span style='color:#22c55e'>chyceno <b>{caught}</b> ({pct})</span> · "
            f"<span style='color:#ef4444'>viděli jsme, ale minuli <b>{seen_missed}</b></span> · "
            f"<span style='color:#9ca3af'>vůbec neviděli {by.get('not_seen', 0)}</span>")
        self._fill_recs(rep)

    def _fill_recs(self, rep):
        while self.rec_box.count():
            w = self.rec_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not rep:
            lab = QLabel("Zatím žádný AI rozbor. Objeví se po prvním scanu, který najde runnery.")
            lab.setWordWrap(True)
            self.rec_box.addWidget(lab)
            self.rec_box.addStretch()
            return
        self.report_id = rep["id"]
        applied = set(json.loads(rep.get("applied") or "[]"))
        head = QLabel(f"<h3>🧠 AI rozbor ({datetime.fromtimestamp(rep['ts']):%d.%m %H:%M}, {rep['n_runners']} runnerů)</h3>"
                      f"<p>{rep.get('summary') or ''}</p>"
                      + (f"<p style='color:#9ca3af'>{rep['notes']}</p>" if rep.get("notes") else "")
                      + f"<p style='color:#6b7280'>{(rep.get('model') or '').split('/')[-1]} · ${rep.get('cost') or 0:.3f}</p>")
        head.setWordWrap(True)
        head.setTextFormat(Qt.RichText)
        self.rec_box.addWidget(head)
        recs = json.loads(rep.get("recommendations") or "[]")
        if not recs:
            self.rec_box.addWidget(QLabel("Žádné změny nastavení AI nedoporučuje."))
        for i, r in enumerate(recs):
            f = QFrame()
            f.setStyleSheet("QFrame{background:#111827;border:1px solid #1f2937;border-radius:8px;}")
            v = QVBoxLayout(f)
            lab = QLabel(f"<b>{r['path']}</b>: {r.get('current')} → <b style='color:#a855f7'>{r['value']}</b>"
                         f" <span style='color:#6b7280'>(jistota {float(r.get('confidence') or 0) * 100:.0f} %)</span><br>"
                         f"{r.get('why', '')}<br><span style='color:#f97316'>⚠ {r.get('risk', '')}</span>")
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.RichText)
            lab.setStyleSheet("border:0")
            v.addWidget(lab)
            b = QPushButton("✓ Použito" if i in applied else "Použít")
            b.setEnabled(i not in applied)
            b.setObjectName("" if i in applied else "start")
            b.clicked.connect(lambda _=False, idx=i: self._apply(idx))
            v.addWidget(b, alignment=Qt.AlignRight)
            self.rec_box.addWidget(f)
        self.rec_box.addStretch()

    def _apply(self, idx):
        from coinscan import runners
        msg = runners.apply(self.report_id, idx)
        self.sig.applied.emit(msg)

    def _applied(self, msg):
        self.cfg_changed.emit()
        self.refresh()
        self.stats.setText(self.stats.text() + f" &nbsp; <span style='color:#22c55e'>{msg}</span>")

    def _row(self, idx):
        return self.model.rows[idx.row()] if idx.isValid() else None

    def _show(self, idx):
        r = self._row(idx)
        if not r:
            return
        tl = json.loads(r.get("timeline") or "{}")
        cross = ", ".join(f"{k} za {v} min" for k, v in (tl.get("cross_min") or {}).items()) or "–"
        at = ", ".join(f"{m} min: {fmt_usd(v)}" for m, v in (tl.get("mcap_at_min") or {}).items()) or "–"
        seen_txt = "–"
        if r.get("first_seen") and r.get("created_ms"):
            seen_txt = (f"{(r['first_seen'] - r['created_ms'] / 1000) / 60:.0f} min po startu při MC "
                        f"{fmt_usd(r.get('first_mcap'))}")
        hot = f"při MC {fmt_usd(r['mcap_at_hot'])}" if r.get("mcap_at_hot") else "nikdy"
        st, col = STATUS.get(r["status"], (r["status"], "#e5e7eb"))
        self.detail.setHtml(
            f"<h3>{r.get('symbol')} <span style='color:#9ca3af'>{r.get('name') or ''}</span></h3>"
            f"<p><b style='color:{col}'>{st}</b>"
            + (f" – poslední důvod: {r['last_reason']}" if r.get("last_reason") else "") + "</p>"
            f"<p>MC teď {fmt_usd(r.get('mcap_now'))} · ATH {fmt_usd(r.get('ath_mcap'))}"
            + (f" · ATH za {tl['ath_at_min']} min od startu" if tl.get("ath_at_min") is not None else "") + "</p>"
            f"<p><b>Poprvé jsme ho viděli:</b> {seen_txt}<br><b>Gem skóre přes práh:</b> {hot} · "
            f"max skóre {r.get('max_score') if r.get('max_score') is not None else '–'}"
            + (f"<br><b>AI gem verdikt:</b> {r['gem_verdict']} při MC {fmt_usd(r.get('gem_mcap'))}" if r.get("gem_verdict") else "")
            + f"</p><p><b>Časová osa:</b> {cross}<br><span style='color:#9ca3af'>{at}</span></p>"
            + (f"<p><b>🧠 AI:</b> {r['ai_why']}<br><b>Poučení:</b> {r.get('ai_lesson') or '–'} "
               f"({'šel chytit' if r.get('catchable') else 'nešel rozumně chytit'})</p>" if r.get("ai_why") else "")
            + f"<p><a style='color:#60a5fa' href='https://pump.fun/coin/{r['mint']}'>pump.fun</a> · "
              f"<a style='color:#60a5fa' href='https://dexscreener.com/solana/{r['mint']}'>DexScreener</a><br>"
              f"<span style='color:#6b7280'>{r['mint']}</span></p>")

    def _open(self, idx):
        r = self._row(idx)
        if r:
            QDesktopServices.openUrl(QUrl(f"https://dexscreener.com/solana/{r['mint']}"))
