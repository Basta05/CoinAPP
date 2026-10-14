"""Záložka 💎 Gemy – všechny coiny, které detektor poslal na AI, jejich report a jak dopadly."""
import json
import threading
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea,
    QSplitter, QTableView, QVBoxLayout, QWidget,
)

from coinscan import gems
from coinscan.config import deep_merge, load_config, save_config

from .gem_popup import report_html
from .widgets import fmt_usd

VCOL = {"BUY": "#22c55e", "WATCH": "#eab308", "SKIP": "#ef4444"}


def row_to_gem(r: dict) -> dict:
    ai = None
    if r.get("verdict"):
        ai = {k: r.get(k) for k in ("verdict", "confidence", "safety", "potential", "summary", "thesis",
                                    "narrative", "model")}
        ai["red_flags"] = json.loads(r.get("red_flags") or "[]")
        ai["strategy"] = json.loads(r.get("strategy") or "{}")
    coin = {"mint": r["address"], "symbol": r.get("symbol"), "name": r.get("name"), "mcap": r.get("mcap0"),
            "progress": r.get("progress0") or 0, "complete": bool(r.get("complete0")),
            "risk_label": r.get("risk_label"), "age_min": None}
    jev = ({"score": r.get("jev_score"), "hint": r.get("jev_action"), "reason": r.get("jev_reason")}
           if r.get("jev_score") is not None else None)
    return {"coin": coin, "ai": ai, "score": r.get("gem_score"), "jev": jev,
            "why": json.loads(r.get("gem_reasons") or "[]")}


ACOL = {"HOLD": "#60a5fa", "ADD": "#22c55e", "TAKE_PROFIT": "#eab308", "SELL": "#ef4444"}
ACT_CZ = {"HOLD": "držet", "ADD": "přikoupit", "TAKE_PROFIT": "vybrat zisk", "SELL": "prodat"}


def track_text(r: dict, deep: int = 120) -> str:
    if r.get("outcome") != "pending":
        return "hotovo"
    if r.get("archived"):
        return "📁 archiv"
    if r.get("verdict") == "BUY":
        cyc = r.get("cycles") or 0
        act = f" · {r['last_action']}" if r.get("last_action") else ""
        return (f"🔄 {cyc}/{deep}" if cyc < deep else "🕐 1 min") + act
    return "⏳ 5 min"


def outcome_text(r: dict) -> tuple[str, str]:
    pm = r.get("peak_mult") or 1
    o = r.get("outcome")
    if o == "pending":
        t = gems.tier(pm)
        return (f"⏳ sleduje se · max {pm:.1f}×" + (f" ({t})" if t else ""), "#60a5fa" if not t else "#22c55e")
    if o and o.endswith("x"):
        return f"🚀 {o.replace('x', '×')}+ (max {pm:.1f}×)", "#22c55e"
    if o == "rug":
        return f"💀 rug (max {pm:.1f}×)", "#ef4444"
    if o == "skip":
        return f"⏹ SKIP – stop po 5 min (max {pm:.1f}×)", "#6b7280"
    if o == "no_ai":
        return f"⏹ bez AI – stop (max {pm:.1f}×)", "#6b7280"
    return f"😐 nic (max {pm:.1f}×)", "#9ca3af"


class GemModel(QAbstractTableModel):
    COLS = ["Čas", "Coin", "💎 Skóre", "🧮 Jev", "AI", "🔄 Sledování", "Bezpečnost", "MC při alertu", "Max ×", "Teď ×", "Výsledek", "Zdroj",
            "Cena AI"]

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.deep = 120

    def rowCount(self, p=QModelIndex()):
        return len(self.rows)

    def columnCount(self, p=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and o == Qt.Horizontal:
            return self.COLS[s]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        r = self.rows[idx.row()]
        col = idx.column()
        if role == Qt.DisplayRole:
            return [
                datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M"),
                r.get("symbol"),
                "" if r.get("gem_score") is None else r["gem_score"],
                "" if r.get("jev_score") is None else f"{r['jev_score']} {r.get('jev_action') or ''}",
                f"{r['verdict']} {r['confidence'] * 100:.0f} %" if r.get("verdict") else "—",
                track_text(r, self.deep),
                r.get("safety") or "",
                fmt_usd(r.get("mcap0")),
                f"{(r.get('peak_mult') or 1):.2f}×",
                f"{(r.get('last_mult') or 1):.2f}×",
                outcome_text(r)[0],
                r.get("source") or "",
                f"${r['cost']:.4f}" if r.get("cost") else "",
            ][col]
        if role == Qt.ForegroundRole:
            if col == 5 and r.get("last_action"):
                return QColor(ACOL.get(r["last_action"], "#e5e7eb"))
            if col == 4 and r.get("verdict"):
                return QColor(VCOL.get(r["verdict"], "#e5e7eb"))
            if col == 10:
                return QColor(outcome_text(r)[1])
            if col == 3 and r.get("jev_score") is not None:
                return QColor("#22c55e" if r["jev_score"] >= 60 else "#eab308" if r["jev_score"] >= 40 else "#ef4444")
            if col == 8 and (r.get("peak_mult") or 0) >= 2:
                return QColor("#22c55e")
        return None

    def set_rows(self, rows):
        self.deep = load_config().get("buy_tracking", {}).get("deep_cycles", 120)
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class _Sig(QObject):
    loaded = Signal(object, object)


class GemsView(QWidget):
    cfg_changed = Signal()

    def __init__(self):
        super().__init__()
        self.sig = _Sig()
        self.sig.loaded.connect(self._fill)
        self._busy = False
        info = QLabel("Sem padá každý coin, který 💎 gem detektor vyhodnotil jako slibný a poslal na AI. "
                      "Appka pak 24 h sleduje, kam až coin od alertu vylétl – tak uvidíš, jestli popupy "
                      "opravdu trefují gemy. <b>Max ×</b> = nejvyšší MC od alertu ÷ MC při alertu.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#9ca3af")
        self.cards = QGridLayout()
        self.card_labels = {}
        for i, key in enumerate(("total", "buy", "2", "5", "10", "20")):
            f = QFrame()
            f.setStyleSheet("QFrame{background:#111827;border:1px solid #1f2937;border-radius:8px;}")
            fl = QVBoxLayout(f)
            big = QLabel("–")
            big.setStyleSheet("font-size:18pt;font-weight:bold;border:0;")
            small = QLabel("")
            small.setStyleSheet("color:#9ca3af;border:0;")
            small.setWordWrap(True)
            fl.addWidget(big)
            fl.addWidget(small)
            self.cards.addWidget(f, 0, i)
            self.card_labels[key] = (big, small)
        self.buy_bar = self._build_buy_bar()
        self.only_buy = QCheckBox("jen BUY")
        self.only_buy.toggled.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(self.only_buy)
        top.addStretch()

        self.model = GemModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.clicked.connect(self._show)
        self.table.doubleClicked.connect(lambda i: self._open("https://pump.fun/coin/{}"))

        self.detail = QLabel("Klikni na gem pro celý report.")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setAlignment(Qt.AlignTop)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.detail)
        btns = QHBoxLayout()
        for text, tpl in (("pump.fun", "https://pump.fun/coin/{}"), ("DexScreener", "https://dexscreener.com/solana/{}"),
                          ("RugCheck", "https://rugcheck.xyz/tokens/{}")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, t=tpl: self._open(t))
            btns.addWidget(b)
        cp = QPushButton("📋 CA")
        cp.clicked.connect(self._copy)
        btns.addWidget(cp)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addLayout(btns)
        rv.addWidget(sc, 1)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([900, 500])

        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addWidget(self.buy_bar)
        lay.addLayout(self.cards)
        lay.addLayout(top)
        lay.addWidget(split, 1)

    # ---------- panel nastavení hlubokého sledování BUY gemů ----------
    def _build_buy_bar(self):
        from PySide6.QtWidgets import QSpinBox
        f = QFrame()
        f.setStyleSheet("QFrame#bb{background:#1e1b4b;border:1px solid #4c1d95;border-radius:8px;}")
        f.setObjectName("bb")
        v = QVBoxLayout(f)
        row = QHBoxLayout()
        b = load_config().get("buy_tracking", {})
        self._bb: dict = {}

        def spin(key, lo, hi, suffix, tip):
            w = QSpinBox()
            w.setRange(lo, hi)
            w.setValue(int(b.get(key, lo)))
            w.setSuffix(suffix)
            w.setToolTip(tip)
            w.valueChanged.connect(self._bb_changed)
            self._bb[key] = w
            return w

        title = QLabel("🔄 <b>BUY gemy</b>")
        title.setTextFormat(Qt.RichText)
        row.addWidget(title)
        for lab, w in (("cyklus", spin("cycle_sec", 10, 600, " s", "Jak často se BUY gem aktualizuje (cena, bez AI)")),
                       ("hluboce", spin("deep_cycles", 1, 2000, " cyklů", "Kolik cyklů sledovat hluboce, pak 1× za minutu")),
                       ("Jev každý", spin("jev_every_cycles", 0, 100, ". cyklus", "Jev skóre každý N-tý cyklus (0 = vypnuto)")),
                       ("max", spin("jev_max_updates", 0, 1000, "× Jev", "Max Jev přepočtů na gem")),
                       ("AI každý", spin("ai_every_cycles", 1, 500, ". cyklus", "AI update (trend, držet/prodat) každý N-tý cyklus")),
                       ("max", spin("ai_max_updates", 0, 100, "× AI", "Max AI updatů na gem"))):
            row.addWidget(QLabel(lab))
            row.addWidget(w)
        self._bb_paid = QCheckBox("placená AI jako pojistka")
        self._bb_paid.setChecked(bool(b.get("ai_allow_paid", False)))
        self._bb_paid.setToolTip("Updaty běží na free AI. Zaškrtni, jestli se při výpadku free má použít placená.")
        self._bb_paid.toggled.connect(self._bb_changed)
        row.addWidget(self._bb_paid)
        row.addStretch()
        v.addLayout(row)
        self._bb_info = QLabel()
        self._bb_info.setStyleSheet("color:#c4b5fd")
        v.addWidget(self._bb_info)
        self._bb_update_info()
        return f

    def _bb_update_info(self):
        g = {k: w.value() for k, w in self._bb.items()}
        cyc = g["cycle_sec"]
        deep_min = g["deep_cycles"] * cyc / 60
        ai_min = g["ai_every_cycles"] * cyc / 60
        ai_n = min(g["ai_max_updates"], g["deep_cycles"] // max(1, g["ai_every_cycles"]))
        jev_n = 0 if not g["jev_every_cycles"] else min(g["jev_max_updates"], g["deep_cycles"] // g["jev_every_cycles"])
        self._bb_info.setText(
            f"= cena každých {cyc} s po dobu {deep_min:.0f} min · Jev každých {g['jev_every_cycles'] * cyc} s "
            f"(max {jev_n}× na gem) · AI update každých {ai_min:.1f} min (max {ai_n}× na gem = trend + držet/prodat) · "
            f"pak 1× za minutu bez AI do 24 h. Ostatní coiny řeší 👁 Watchlist (SHIT / LONG / SHORT).")

    def _bb_changed(self, *_):
        self._bb_update_info()
        vals = {k: w.value() for k, w in self._bb.items()}
        vals["ai_allow_paid"] = self._bb_paid.isChecked()
        save_config(deep_merge(load_config(), {"buy_tracking": vals}))
        self.cfg_changed.emit()

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        only = self.only_buy.isChecked()

        def work():
            try:
                rows, summ = gems.list_gems(only_buy=only), gems.summary()
            except Exception:  # noqa: BLE001
                rows, summ = [], None
            self.sig.loaded.emit(rows, summ)

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, rows, s):
        self._busy = False
        self.model.set_rows(rows)
        if not s:
            return
        pct = lambda a, b: f"{a / b * 100:.0f} %" if b else "–"  # noqa: E731
        self.card_labels["total"][0].setText(str(s["total"]))
        self.card_labels["total"][1].setText(f"gemů celkem · vyhodnoceno {s['evaluated']}")
        self.card_labels["buy"][0].setText(str(s["buy"]))
        self.card_labels["buy"][1].setText(f"AI řekla BUY (popup) · WATCH {s.get('watch', 0)}\n"
                                           f"AI celkem ${s.get('cost', 0):.2f}")
        for t in (2, 5, 10, 20):
            n, nb = s["tiers"][t], s["buy_tiers"][t]
            self.card_labels[str(t)][0].setText(f"{n}")
            self.card_labels[str(t)][1].setText(f"došlo na {t}×+ ({pct(n, s['total'])})\n"
                                                f"z BUY: {nb} ({pct(nb, s['buy'])})")

    def _sel(self):
        idx = self.table.currentIndex()
        return self.model.rows[idx.row()] if idx.isValid() else None

    def _show(self, idx):
        r = self.model.rows[idx.row()]
        g = row_to_gem(r)
        try:
            g["screen"] = json.loads(r.get("screen") or "{}")
        except ValueError:
            pass
        if g.get("ai") is not None:
            g["ai"]["paid"] = bool(r.get("paid"))
        txt, col = outcome_text(r)
        head = (f"<h2>💎 {r.get('symbol')} <span style='color:#9ca3af;font-size:10pt'>{r.get('name') or ''}</span></h2>"
                f"<p>Alert {datetime.fromtimestamp(r['ts']):%d.%m %H:%M} při MC {fmt_usd(r.get('mcap0'))} · "
                f"teď {fmt_usd(r.get('last_mcap'))} · max {fmt_usd(r.get('peak_mcap'))} · "
                f"<b style='color:{col}'>{txt}</b>"
                + (f" · <span style='color:#6b7280'>{r['stop_reason']}</span>" if r.get("stop_reason") else "")
                + (f" · AI ${r['cost']:.4f}" if r.get("cost") else "") + "</p>")
        tl = ""
        try:
            ups = gems.get_updates(r["id"], ("jev", "ai"))
        except Exception:  # noqa: BLE001
            ups = []
        ai_ups = [u for u in ups if u["kind"] == "ai"]
        jev_ups = [u for u in ups if u["kind"] == "jev"]
        if ai_ups or jev_ups:
            tl = f"<h3>🔄 Vývoj (cyklus {r.get('cycles') or 0})</h3>"
            for u in reversed(ai_ups):
                d = json.loads(u["data"] or "{}")
                col = ACOL.get(u["action"], "#e5e7eb")
                tl += (f"<p><b style='color:{col}'>{u['action']} ({ACT_CZ.get(u['action'], '')})</b> "
                       f"<span style='color:#9ca3af'>{datetime.fromtimestamp(u['ts']):%H:%M} · cyklus {u['cycle']} · "
                       f"{(u['mult'] or 1):.2f}× · trend {d.get('trend', '?')}"
                       + (" · vyplatí se vstoupit" if d.get("worth") else "") + f"</span><br>{d.get('summary', '')}"
                       + (f"<br><span style='color:#9ca3af'>{d['levels']}</span>" if d.get("levels") else "") + "</p>")
            if jev_ups:
                pts = " → ".join(str(u["jev_score"]) for u in jev_ups[-15:])
                tl += f"<p><b>🧮 Jev skóre:</b> {r.get('jev0') if r.get('jev0') is not None else '–'} (alert) … {pts}</p>"
        self.detail.setText(head + tl + report_html(g) + f"<p style='color:#6b7280'>model: {r.get('model') or '—'}<br>"
                            f"CA: {r['address']}</p>")

    def _open(self, tpl):
        r = self._sel()
        if r:
            QDesktopServices.openUrl(QUrl(tpl.format(r["address"])))

    def _copy(self):
        r = self._sel()
        if r:
            QGuiApplication.clipboard().setText(r["address"])
