"""Pulse obrazovka – 3 sloupce (New / Soon migrating / Migrated) + live detail s candle grafem."""
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from coinscan import llm, riskmgmt, rugcheck
from coinscan.candles import TIMEFRAMES
from coinscan.pulse import COLUMNS, PulseStore, passes, sort_key

from .candle_chart import CandleChart
from .widgets import RISK_COL, CoinDelegate, CoinListModel, fmt_age, fmt_usd

TITLES = {"new": "🌱 NEW", "soon": "🔥 SOON MIGRATING", "migrated": "🎓 MIGRATED"}


class _Sig(QObject):
    ai_done = Signal(str, str)  # mint, html


class PulseView(QWidget):
    def __init__(self, cfg: dict, log):
        super().__init__()
        self.cfg = cfg
        self.store = PulseStore(cfg, log)
        self.sig = _Sig()
        self.sig.ai_done.connect(self._ai_done)
        self.ai_cache: dict[str, str] = {}
        self.max_items = 50
        self._seen_version = -1
        self._chart_version = -1
        self._quick_dirty = True
        self._detail_mint = None
        self.tf = cfg.get("pulse", {}).get("default_timeframe", "15s")

        # ---- rychlé filtry ----
        self.btn = QPushButton("▶  Spustit Pulse")
        self.btn.setObjectName("start")
        self.btn.clicked.connect(self.toggle)
        self.search = QLineEdit(placeholderText="Hledat symbol / název / CA…")
        self.min_mcap = QSpinBox()
        self.min_mcap.setRange(0, 100_000_000)
        self.min_mcap.setSingleStep(5000)
        self.min_mcap.setPrefix("min MC $")
        self.socials = QCheckBox("jen se socials")
        self.hide_high = QCheckBox("skrýt HIGH risk")
        self.show_bots = QCheckBox("🤖 ukázat boty")
        self.show_bots.setToolTip("Bot filtr skrývá fake coiny (schody, pila, stejné objemy, wash trading).\n"
                                  "Zaškrtni pro kontrolu – boti se ukážou červeně.")
        self.status = QLabel("zastaveno")
        self.status.setStyleSheet("color:#6b7280")
        bar = QHBoxLayout()
        bar.addWidget(self.btn)
        bar.addWidget(self.search, 2)
        bar.addWidget(self.min_mcap)
        bar.addWidget(self.socials)
        bar.addWidget(self.hide_high)
        bar.addWidget(self.show_bots)
        bar.addStretch()
        bar.addWidget(self.status)
        for w in (self.search.textChanged, self.min_mcap.valueChanged, self.socials.toggled, self.hide_high.toggled,
                  self.show_bots.toggled):
            w.connect(self._mark_dirty)

        # ---- 3 sloupce ----
        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        self.models, self.views, self.headers = {}, {}, {}
        for col in COLUMNS:
            box = QVBoxLayout()
            h = QLabel(TITLES[col])
            h.setStyleSheet("font-weight:bold; font-size:11pt; padding:4px;")
            model = CoinListModel(col)
            view = QListView()
            view.setModel(model)
            view.setItemDelegate(CoinDelegate(col, view))
            view.setMouseTracking(True)
            view.setUniformItemSizes(True)
            view.setVerticalScrollMode(QListView.ScrollPerPixel)
            view.clicked.connect(lambda idx: self.select(idx.data(Qt.UserRole)))
            view.doubleClicked.connect(lambda idx: self._open(f"https://pump.fun/coin/{idx.data(Qt.UserRole)['mint']}"))
            box.addWidget(h)
            box.addWidget(view)
            cols.addLayout(box)
            self.models[col], self.views[col], self.headers[col] = model, view, h
        cols_w = QWidget()
        cols_w.setLayout(cols)

        self.detail = self._build_detail()
        split = QSplitter(Qt.Horizontal)
        split.addWidget(cols_w)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([950, 620])

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        # seznamy max 2×/s, graf 4×/s – ale jen když se něco změnilo
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.chart_timer = QTimer(self)
        self.chart_timer.timeout.connect(self.refresh_chart)
        self.chart_timer.start(250)

    # ------------------------------------------------------------ detail panel
    def _build_detail(self) -> QWidget:
        w = QFrame()
        w.setMinimumWidth(420)
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)
        self.d_title = QLabel("Klikni na coin")
        self.d_title.setStyleSheet("font-size:14pt; font-weight:bold;")
        self.d_title.setTextFormat(Qt.RichText)
        self.d_ca = QLabel()
        self.d_ca.setStyleSheet("color:#6b7280; font-size:8pt;")
        self.d_ca.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.d_title)
        v.addWidget(self.d_ca)

        # timeframe přepínač
        tf_row = QHBoxLayout()
        tf_row.setSpacing(2)
        self.tf_group = QButtonGroup(self)
        for tf in TIMEFRAMES:
            b = QPushButton(tf)
            b.setCheckable(True)
            b.setObjectName("tf")
            b.setFixedWidth(42)
            if tf == self.tf:
                b.setChecked(True)
            self.tf_group.addButton(b)
            b.clicked.connect(lambda _=False, t=tf: self.set_tf(t))
            tf_row.addWidget(b)
        tf_row.addStretch()
        v.addLayout(tf_row)

        self.chart = CandleChart()
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.chart)

        lower = QWidget()
        lv = QVBoxLayout(lower)
        lv.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        grid.setVerticalSpacing(0)
        self.d_stats = {}
        keys = [("mcap", "MCap"), ("ath", "ATH"), ("prog", "Bonding"), ("age", "Stáří"),
                ("liq", "Likvidita"), ("vol", "Objem 1h"), ("txns", "Buy/Sell 5m"), ("chg", "5m / 1h"),
                ("risk", "Risk"), ("top10", "Top10 / insideři"), ("sol", "SOL v curve"), ("replies", "Komentáře")]
        for i, (k, label) in enumerate(keys):
            lab = QLabel(label)
            lab.setStyleSheet("color:#6b7280; font-size:8pt;")
            val = QLabel("–")
            val.setStyleSheet("font-weight:bold;")
            val.setTextFormat(Qt.RichText)
            grid.addWidget(lab, (i // 4) * 2, i % 4)
            grid.addWidget(val, (i // 4) * 2 + 1, i % 4)
            self.d_stats[k] = val
        lv.addLayout(grid)

        btns = QHBoxLayout()
        self.b_pump = QPushButton("pump.fun")
        self.b_dex = QPushButton("DexScreener")
        self.b_copy = QPushButton("📋 CA")
        self.b_rc = QPushButton("🛡 RugCheck")
        self.b_ai = QPushButton("🤖 AI analýza")
        self.b_ai.setToolTip("Ruční analýza i pro low coiny: Jev skóre + report (free, placený jen když free nejde)")
        self.b_ai.setObjectName("start")
        for b in (self.b_pump, self.b_dex, self.b_copy, self.b_rc, self.b_ai):
            btns.addWidget(b)
        self.b_pump.clicked.connect(lambda: self._open_sel("https://pump.fun/coin/{}"))
        self.b_dex.clicked.connect(lambda: self._open_sel("https://dexscreener.com/solana/{}"))
        self.b_rc.clicked.connect(lambda: self._open_sel("https://rugcheck.xyz/tokens/{}"))
        self.b_copy.clicked.connect(self._copy)
        self.b_ai.clicked.connect(self.run_ai)
        lv.addLayout(btns)

        self.d_info = QLabel()
        self.d_info.setWordWrap(True)
        self.d_info.setTextFormat(Qt.RichText)
        self.d_info.setOpenExternalLinks(True)
        self.d_info.setAlignment(Qt.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.d_info)
        scroll.setFrameShape(QFrame.NoFrame)
        lv.addWidget(scroll, 1)
        split.addWidget(lower)
        split.setSizes([420, 330])
        v.addWidget(split, 1)
        return w

    # ------------------------------------------------------------ řízení
    def toggle(self):
        if self.store.running():
            self.store.stop()
            self.btn.setText("▶  Spustit Pulse")
            self.btn.setObjectName("start")
        else:
            self.store.start()
            self.btn.setText("■  Zastavit Pulse")
            self.btn.setObjectName("stop")
        self.btn.setStyle(self.btn.style())

    def update_cfg(self, cfg: dict):
        self.cfg = cfg
        self.store.update_cfg(cfg)
        self._mark_dirty()

    def _mark_dirty(self, *_):
        self._quick_dirty = True

    def quick(self) -> dict:
        bc = self.cfg.get("bot_filter", {})
        on = bc.get("enabled", True) and not self.show_bots.isChecked()
        return {"search": self.search.text(), "min_mcap": self.min_mcap.value(),
                "socials_only": self.socials.isChecked(), "hide_high": self.hide_high.isChecked(),
                "hide_bots": on and bc.get("hide", True),
                "hide_unchecked": on and bc.get("hide_unchecked", True)}

    def set_tf(self, tf: str):
        self.tf = tf
        if self.store.selected:
            self.store.select(self.store.selected, tf)
        self._chart_version = -1

    # ------------------------------------------------------------ refresh
    def refresh(self):
        if not self.isVisible():
            return
        version, snap = self.store.snapshot()
        self._update_status(len(snap))
        if version == self._seen_version and not self._quick_dirty:
            return
        self._seen_version, self._quick_dirty = version, False

        cols_cfg = self.cfg.get("pulse", {}).get("columns", {})
        q = self.quick()
        buckets = {c: [] for c in COLUMNS}
        now = time.time()
        bot_cols = self.cfg.get("bot_filter", {}).get("columns", list(COLUMNS))
        min_age = self.cfg.get("bot_filter", {}).get("min_age_sec", 30)
        q_nobot = {**q, "hide_bots": False, "hide_unchecked": False}
        hidden_bots = 0
        for c in snap.values():
            col = self.store.column_of(c)
            cc = cols_cfg.get(col, {})
            qq = q if col in bot_cols else q_nobot
            # úplně čerstvé coiny (< min stáří) bot filtr ještě nekontroluje -> neschovávat kvůli tomu
            if qq["hide_unchecked"] and (now * 1000 - c["created_ms"]) / 1000 < min_age:
                qq = {**qq, "hide_unchecked": False}
            if col == "migrated" and c.get("migrated_at"):
                # u migrovaných počítáme stáří od migrace
                if cc.get("max_age_min") and (now - c["migrated_at"]) / 60 > cc["max_age_min"]:
                    continue
                cc = {**cc, "max_age_min": 0}
            if passes(c, cc, qq):
                buckets[col].append(c)
            elif (c.get("pat") or {}).get("verdict") == "bot" and passes(c, cc, q_nobot):
                hidden_bots += 1
        self.show_bots.setText(f"🤖 ukázat boty ({hidden_bots} skryto)" if hidden_bots else "🤖 ukázat boty")
        for col, items in buckets.items():
            items.sort(key=lambda c, col=col: sort_key(col, c))
            items = items[: self.max_items]
            view, model = self.views[col], self.models[col]
            sb = view.verticalScrollBar().value()
            model.set_items(items)
            view.verticalScrollBar().setValue(sb)
            row = model.row_of(self.store.selected) if self.store.selected else -1
            if row >= 0:
                view.setCurrentIndex(model.index(row))
            self.headers[col].setText(f"{TITLES[col]}  ({len(items)})")
        if self.store.selected in snap:
            self.update_detail(snap[self.store.selected])

    def _update_status(self, n: int):
        if self.store.running():
            ago = time.time() - self.store.last_update if self.store.last_update else None
            parts = [f"● live · {n} coinů"]
            if ago is not None:
                parts.append(f"update před {ago:.0f}s ({self.store.poll_ms:.0f} ms)")
            parts.append("WS ✓" if self.store.ws.connected else "WS ✗")
            warn = ""
            if self.store.last_error:
                warn += f" | ⚠ {self.store.last_error[:50]}"
            if rugcheck.cooldown_left() > 0:
                warn += f" | ⚠ RugCheck pauza {rugcheck.cooldown_left():.0f}s"
            self.status.setText(" · ".join(parts) + warn)
            self.status.setStyleSheet(f"color:{'#ef4444' if warn else '#22c55e'}")
        else:
            self.status.setText("zastaveno")
            self.status.setStyleSheet("color:#6b7280")

    def refresh_chart(self):
        if not self.isVisible():
            return
        version, tf, candles, loading, err = self.store.chart.get()
        if version == self._chart_version:
            return
        self._chart_version = version
        msg = "načítám graf…" if loading else err
        self.chart.set_data(candles, TIMEFRAMES[tf], msg)

    def select(self, c: dict):
        self.store.select(c["mint"], self.tf)
        self._chart_version = -1
        self.chart.set_data([], TIMEFRAMES[self.tf], "načítám graf…")
        self.update_detail(c)
        for col, view in self.views.items():
            row = self.models[col].row_of(c["mint"])
            if row < 0:
                view.clearSelection()

    def update_detail(self, c: dict | None):
        if not c:
            return
        self.d_title.setText(f"{c['symbol']}  <span style='color:#9ca3af;font-size:10pt'>{c['name']}</span>")
        self.d_ca.setText(c["mint"])
        d = c.get("dex") or {}
        pc = d.get("price_change") or {}
        rc = c.get("rc") or {}
        rl = c.get("risk_label") or "?"
        vals = {
            "mcap": fmt_usd(c["mcap"]),
            "ath": f"{fmt_usd(c['ath_mcap'])} ({(c['mcap'] / c['ath_mcap'] * 100 if c['ath_mcap'] else 0):.0f}%)",
            "prog": "MIGROVÁNO" if c["complete"] else f"{c['progress']:.1f} %",
            "age": fmt_age(c["age_min"]),
            "sol": f"{c['sol_in_curve']:.1f}" if not c["complete"] else "–",
            "replies": str(c["replies"]),
            "liq": fmt_usd(d.get("liquidity")) if d else "–",
            "vol": fmt_usd(d.get("volume_h1")) if d else "–",
            "txns": f"{d.get('buys_m5', 0)} / {d.get('sells_m5', 0)}" if d else "–",
            "chg": f"{pc.get('m5', 0):+.0f}% / {pc.get('h1', 0):+.0f}%" if d else "–",
            "risk": f"<span style='color:{RISK_COL.get(rl)}'>{rl} {c.get('risk_score') or ''}</span>",
            "top10": f"{rc.get('top10_pct', '?')}% / {rc.get('insiders', '?')}" if rc else "čekám…",
        }
        for k, v in vals.items():
            self.d_stats[k].setText(v)

        # spodní info přestavujeme jen při změně coinu nebo risku (drahé na layout)
        key = (c["mint"], rl, bool(rc), self.ai_cache.get(c["mint"]), round(c["mcap"], -3), c.get("gem"),
               (c.get("jev") or {}).get("score"),
               bool(c.get("ai")),
               (c.get("pat") or {}).get("score"), (c.get("pat") or {}).get("verdict"))
        if key == self._detail_mint:
            return
        self._detail_mint = key
        plan = riskmgmt.plan(self.cfg, rl, c["mcap"])
        links = " · ".join(f"<a href='{u}' style='color:#60a5fa'>{n}</a>"
                           for n, u in (("𝕏", c["twitter"]), ("Telegram", c["telegram"]), ("Web", c["website"])) if u)
        risks = "".join(f"<br>• [{r['level']}] {r['name']}" for r in rc.get("risks", [])[:8]) if rc else ""
        flags = ""
        if rc:
            flags = (f"mint auth: {'⚠ ANO' if rc.get('mint_authority') else 'ne'} · "
                     f"freeze: {'⚠ ANO' if rc.get('freeze_authority') else 'ne'} · RC skóre {rc.get('score')}")
        html = ""
        pat = c.get("pat")
        if pat:
            col = {"bot": "#f87171", "ok": "#22c55e"}.get(pat["verdict"], "#9ca3af")
            label = {"bot": "🤖 BOT / fake objem", "ok": "✓ bez bot vzorů", "unknown": "? zatím málo dat"}[pat["verdict"]]
            why = "".join(f"<br>• {r}" for r in pat.get("reasons", []))
            html += (f"<p><b>Bot filtr</b> <span style='color:{col};font-weight:bold'>{label}</span> "
                     f"(skóre {pat.get('score', 0)}, {pat.get('n', 0)} s s obchody){why}</p>")
        if c.get("gem") is not None:
            g = self.cfg.get("gems", {})
            tier = ("BUY" if c["gem"] >= g.get("buy_score", 65) else "WATCH" if c["gem"] >= g.get("watch_score", 50)
                    else "low – AI jen tlačítkem")
            html += (f"<p><b>💎 Gem skóre {c['gem']}</b> ({tier}) "
                     f"<span style='color:#93c5fd'>{', '.join(c.get('gem_why') or []) or '—'}</span></p>")
        if c.get("jev"):
            j = c["jev"]
            col = "#22c55e" if j["score"] >= 60 else "#eab308" if j["score"] >= 40 else "#ef4444"
            html += (f"<p><b>🧮 Jev skóre <span style='color:{col}'>{j['score']}</span></b> – {j['reason']} "
                     f"<span style='color:#6b7280'>(bezpečnost {j['safety']}, potenciál {j['potential']})</span></p>")
        elif c.get("jev_error"):
            html += f"<p style='color:#6b7280'>Jev: {c['jev_error']}</p>"
        if c["mint"] in self.ai_cache:
            html += f"<p><b>🤖 AI analýza</b><br>{self.ai_cache[c['mint']]}</p>"
        elif c.get("ai"):
            from .gem_popup import report_html
            html += ("<p><b>🤖 Auto AI report</b><br>"
                     + report_html({"coin": c, "ai": c["ai"], "score": None, "why": [], "plan": None}) + "</p>")
        html += (f"<p>{links or '<span style=color:#6b7280>bez socials</span>'}</p>"
                 f"<p><b>Risk management</b><br>{riskmgmt.fmt_plan(plan)}</p>"
                 f"<p><b>RugCheck</b><br>{flags or 'zatím nezkontrolováno'}{risks}</p>")
        if c.get("description"):
            html += f"<p><b>Popis</b><br><span style='color:#9ca3af'>{c['description'][:400]}</span></p>"
        self.d_info.setText(html)

    # ------------------------------------------------------------ akce
    def _open(self, url):
        QDesktopServices.openUrl(QUrl(url))

    def _open_sel(self, tpl):
        if self.store.selected:
            self._open(tpl.format(self.store.selected))

    def _copy(self):
        if self.store.selected:
            QGuiApplication.clipboard().setText(self.store.selected)
            self.b_copy.setText("✓")
            QTimer.singleShot(1200, lambda: self.b_copy.setText("📋 CA"))

    def run_ai(self):
        mint = self.store.selected
        c = self.store.snapshot()[1].get(mint) if mint else None
        if not c:
            return
        self.ai_cache[mint] = "<i>analyzuji… Jev + free model (může trvat až 30 s, placený jen když free nejde)</i>"
        self._detail_mint = None
        self.update_detail(c)

        def work():
            try:
                d = c.get("dex") or {}
                metrics = {
                    "symbol": c["symbol"], "name": c["name"], "age_min": c["age_min"], "mcap": c["mcap"],
                    "price": None, "liquidity": d.get("liquidity"), "volume_h1": d.get("volume_h1"),
                    "buys_m5": d.get("buys_m5"), "sells_m5": d.get("sells_m5"),
                    "buys_h1": d.get("buys_h1"), "sells_h1": d.get("sells_h1"),
                    "price_change": d.get("price_change"), "dex_id": d.get("dex_id") or "pumpfun",
                    "websites": [c["website"]] if c["website"] else [],
                    "socials": [{"type": t, "url": c[t]} for t in ("twitter", "telegram") if c[t]],
                    "bonding_progress_pct": c["progress"], "migrated": c["complete"],
                    "ath_mcap": c["ath_mcap"], "replies": c["replies"],
                }
                profile = {"description": c.get("description", ""), "links": []}
                profile, metrics, rc = self.store._ai_input(c, c.get("gem") or 0, c.get("gem_why") or [])
                from coinscan.config import env as _env
                if not c.get("jev") and llm.has_jev():
                    try:
                        c["jev"] = llm.jev_screen(profile, metrics, rc, self.cfg)
                        with self.store.lock:
                            if c["mint"] in self.store.coins:
                                self.store.coins[c["mint"]] = dict(self.store.coins[c["mint"]], jev=c["jev"],
                                                                   jev_ts=time.time())
                    except Exception:  # noqa: BLE001
                        pass
                # ruční analýza: free hlavní, placený pojistka
                ai = llm.report(profile, metrics, rc, self.cfg)
                from coinscan import riskmgmt as _rm
                from .gem_popup import report_html
                html = report_html({"coin": c, "ai": ai, "score": c.get("gem"), "why": c.get("gem_why") or [],
                                    "jev": c.get("jev"),
                                    "plan": _rm.plan(self.cfg, c.get("risk_label"), c.get("mcap"))})
                html += f"<br><span style='color:#6b7280'>model: {ai.get('model', '')}</span>"
            except Exception as e:  # noqa: BLE001
                html = f"<span style='color:#ef4444'>Chyba: {e}</span>"
            self.sig.ai_done.emit(mint, html)

        threading.Thread(target=work, daemon=True).start()

    def _ai_done(self, mint, html):
        self.ai_cache[mint] = html
        self._detail_mint = None
        c = self.store.snapshot()[1].get(mint)
        if c and mint == self.store.selected:
            self.update_detail(c)

    def shutdown(self):
        self.store.shutdown()
