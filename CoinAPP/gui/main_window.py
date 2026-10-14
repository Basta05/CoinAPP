import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from coinscan import db, gems, llm  # noqa: F401
from coinscan import status as coin_status
from coinscan.config import CONFIG_PATH, ROOT, load_config
from coinscan.engine import Engine

from .coins_view import CoinsView
from .gem_popup import PopupManager
from .gems_view import GemsView
from .pulse_view import PulseView
from .risk_view import RiskView
from .runners_view import RunnersView
from .watch_view import WatchView
from .settings_view import SettingsView
from .stats_view import StatsView

LEVEL_COLORS = {"INFO": "#9aa4b2", "SKIP": "#6b7280", "HIT": "#22c55e", "ERROR": "#ef4444"}
TAB_PULSE, TAB_WATCH, TAB_GEMS, TAB_RUNNERS, TAB_LOG, TAB_COINS, TAB_STATS, TAB_SET, TAB_RISK = range(9)


class Bridge(QObject):
    log = Signal(str, str)
    changed = Signal()
    finished = Signal()
    gem = Signal(object)
    runners_done = Signal()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CoinScan – Solana memecoin scanner")
        self.resize(1500, 900)
        db.init()

        self.bridge = Bridge()
        self.bridge.log.connect(self._queue_log)
        self.bridge.changed.connect(self.refresh_current)
        self.bridge.finished.connect(self.on_finished)
        self.bridge.gem.connect(self.show_gem)
        self.log_path = ROOT / "data" / "app.log"
        self.log_path.parent.mkdir(exist_ok=True)
        self._log_buf: list[tuple[str, str, str]] = []
        self.engine: Engine | None = None
        self.thread: threading.Thread | None = None
        self.popups = PopupManager(self)

        # --- horní lišta ---
        self.btn_start = QPushButton("▶  Start scanner")
        self.btn_start.setObjectName("start")
        self.btn_start.clicked.connect(self.toggle)
        self.status = QLabel("Scanner zastaven")
        btn_cfg = QPushButton("⚙  Config")
        btn_cfg.clicked.connect(lambda: _open_path(CONFIG_PATH))
        btn_env = QPushButton("🔑  .env")
        btn_env.clicked.connect(self.open_env)
        self.api_status = QLabel()
        self.api_status.setTextFormat(Qt.RichText)
        self.gem_status = QLabel()
        self.gem_status.setStyleSheet("color:#a855f7")
        top = QHBoxLayout()
        for w in (self.btn_start, self.status):
            top.addWidget(w)
        top.addSpacing(16)
        top.addWidget(self.gem_status)
        top.addStretch()
        top.addWidget(self.api_status)
        top.addSpacing(12)
        for w in (btn_cfg, btn_env):
            top.addWidget(w)

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont("Consolas", 9))

        cfg = load_config()
        self.pulse = PulseView(cfg, self.bridge.log.emit)
        self.pulse.store.on_gem = self.bridge.gem.emit
        self.pulse.store.on_gem_update = self.bridge.gem.emit
        self.watchv = WatchView(self.pulse.store)
        self.watchv.open_coin.connect(self.focus_coin)
        self.gems = GemsView()
        self.runners = RunnersView()
        self.runners.scan_now.connect(lambda: self._runner_wake.set())
        self.runners.cfg_changed.connect(lambda: self.on_cfg_saved({}))
        self.gems.cfg_changed.connect(lambda: self.pulse.update_cfg(load_config()))
        self.bridge.runners_done.connect(self._runners_done)
        self.coins = CoinsView()
        self.stats = StatsView()
        self.settings = SettingsView()
        self.settings.saved.connect(self.on_cfg_saved)
        self.risk = RiskView()
        self.risk.saved.connect(self.on_cfg_saved)

        tabs = self.tabs = QTabWidget()
        tabs.addTab(self.pulse, "⚡ Pulse")
        tabs.addTab(self.watchv, "👁 Watchlist")
        tabs.addTab(self.gems, "💎 Gemy")
        tabs.addTab(self.runners, "🏁 Runneři")
        tabs.addTab(self.log_view, "Scanner log")
        tabs.addTab(self.coins, "Coiny (DB)")
        tabs.addTab(self.stats, "Úspěšnost")
        tabs.addTab(self.settings, "⚙ Filtry && presety")
        tabs.addTab(self.risk, "🛡 Risk management")
        tabs.currentChanged.connect(lambda _: self.refresh_current())

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addLayout(top)
        lay.addWidget(tabs)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)

        # časovače: DB data jen pro otevřenou záložku, log dávkově, stav API 1×/s
        t1 = QTimer(self)
        t1.timeout.connect(self.refresh_current)
        t1.start(30_000)
        t2 = QTimer(self)
        t2.timeout.connect(self.update_status_bar)
        t2.start(1000)
        t3 = QTimer(self)
        t3.timeout.connect(self._flush_log)
        t3.start(400)
        # watchdog: změří, jestli se GUI nezaseklo, a zapíše to do logu (ať víme kde)
        self._wd_last = time.perf_counter()
        wd = QTimer(self)
        wd.timeout.connect(self._watchdog)
        wd.start(200)
        # sledování gemů (2×/5×/10×/20×) – na pozadí každé 2 min
        threading.Thread(target=self._gem_tracker, daemon=True).start()
        # runner scan (velké coiny vs. naše gemy) – každých X min + tlačítko "Skenovat teď"
        self._runner_wake = threading.Event()
        threading.Thread(target=self._runner_loop, daemon=True).start()

        try:
            from coinscan import llm as _llm
            self._queue_log("INFO", "🤖 AI z .env → " + _llm.tiers_info())
        except Exception:  # noqa: BLE001
            pass
        if not (ROOT / ".env").exists():
            self._queue_log("ERROR", "Chybí soubor .env – klikni na '🔑 .env' a doplň klíče a modely (OPENROUTER_API_KEY_FREE + OPENROUTER_FREE_MODELS, …).")

    # ---------- start / stop ----------
    def toggle(self):
        if self.thread and self.thread.is_alive():
            self.engine.stop()
            self.btn_start.setEnabled(False)
            self.status.setText("Zastavuji…")
            return
        self.engine = Engine(log=self.bridge.log.emit, on_change=self.bridge.changed.emit)
        self.engine.on_gem = self.bridge.gem.emit
        self.engine.on_candidate = self.pulse.store.inject_async
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.btn_start.setText("■  Stop scanner")
        self.btn_start.setObjectName("stop")
        self.btn_start.setStyle(self.btn_start.style())
        self.status.setText("Scanner běží")

    def _run(self):
        try:
            self.engine.run()
        finally:
            self.bridge.finished.emit()

    def on_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_start.setText("▶  Start scanner")
        self.btn_start.setObjectName("start")
        self.btn_start.setStyle(self.btn_start.style())
        self.status.setText("Scanner zastaven")

    # ---------- gemy ----------
    def show_gem(self, gem: dict):
        g = load_config().get("gems", {})
        self.popups.show(gem, g.get("popup_seconds", 90), g.get("popup_sound", True), self.focus_coin)
        if self.tabs.currentIndex() == TAB_GEMS:
            self.gems.refresh()

    def focus_coin(self, mint: str):
        """Z popupu 'Graf' -> otevřít Pulse s tímhle coinem."""
        self.tabs.setCurrentIndex(TAB_PULSE)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        _, snap = self.pulse.store.snapshot()
        c = snap.get(mint) or {"mint": mint, "symbol": "?", "name": "", "created_ms": None, "mcap": 0,
                               "ath_mcap": 0, "progress": 0, "complete": False, "age_min": 0, "sol_in_curve": 0,
                               "replies": 0, "twitter": "", "telegram": "", "website": ""}
        self.pulse.select(c)

    def _gem_tracker(self):
        """BUY/WATCH gemy každou minutu, ostatní po 5 min; bez AI -> retry přes placenou AI."""
        while True:
            try:
                gems.track_once(load_config(), retry=self.pulse.store.retry_gem)
            except Exception as e:  # noqa: BLE001
                self.bridge.log.emit("ERROR", f"Gem tracker: {e}")
            time.sleep(20)

    def _runner_loop(self):
        from coinscan import runners
        self._runner_wake.wait(120)       # první scan 2 min po startu (ať Pulse nasbírá data)
        while True:
            self._runner_wake.clear()
            cfg = load_config()
            if cfg.get("runners", {}).get("enabled", True):
                try:
                    _, snap = self.pulse.store.snapshot()
                    runners.scan(cfg, list(snap.values()), self.bridge.log.emit)
                except Exception as e:  # noqa: BLE001
                    self.bridge.log.emit("ERROR", f"Runner scan: {e}")
            self.bridge.runners_done.emit()
            self._runner_wake.wait(cfg.get("runners", {}).get("interval_min", 10) * 60)

    def _runners_done(self):
        self.runners.set_scanning(False)
        if self.tabs.currentIndex() == TAB_RUNNERS:
            self.runners.refresh()

    # ---------- log (dávkově – desítky řádků za sekundu jinak zasekávaly GUI) ----------
    def _queue_log(self, level: str, msg: str):
        self._log_buf.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg))

    def _flush_log(self):
        if not self._log_buf:
            return
        buf, self._log_buf = self._log_buf, []
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 2_000_000:
                self.log_path.replace(self.log_path.with_suffix(".old.log"))
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.writelines(f"{ts} [{lv}] {m}\n" for ts, lv, m in buf)
        except OSError:
            pass
        html = []
        for ts, lv, m in buf[-300:]:
            col = LEVEL_COLORS.get(lv, "#e5e7eb")
            safe = m.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
            html.append(f'<span style="color:#4b5563">{ts[11:]}</span> <b style="color:{col}">[{lv}]</b> '
                        f'<span style="color:{col}">{safe}</span>')
        self.log_view.appendHtml("<br>".join(html))
        self.log_view.moveCursor(QTextCursor.End)

    def _watchdog(self):
        now = time.perf_counter()
        lag = now - self._wd_last - 0.2
        self._wd_last = now
        if lag > 0.5:
            self._queue_log("ERROR", f"GUI zaseknuté {lag:.1f} s (záložka: {self.tabs.tabText(self.tabs.currentIndex())})")

    # ---------- data ----------
    def refresh_current(self):
        i = self.tabs.currentIndex()
        if i == TAB_COINS:
            self.coins.refresh()
        elif i == TAB_STATS:
            self.stats.refresh()
        elif i == TAB_GEMS:
            self.gems.refresh()
        elif i == TAB_RUNNERS:
            self.runners.refresh()

    def update_status_bar(self):
        st = coin_status.get()
        parts, tips = [], []
        for name in coin_status.NAMES:
            d = st.get(name)
            if not d:
                continue
            stale = time.time() - d["ts"] > 120
            col = "#6b7280" if stale else "#22c55e" if d["ok"] else "#ef4444"
            lat = f" {d['lat'] * 1000:.0f}ms" if d.get("lat") and d["ok"] else ""
            parts.append(f"<span style='color:{col}'>● {name}{lat}</span>")
            tips.append(f"{name}: {'OK' if d['ok'] else 'CHYBA'} {d['msg']} (před {time.time() - d['ts']:.0f}s)")
        self.api_status.setText("&nbsp;&nbsp;".join(parts))
        self.api_status.setToolTip("\n".join(tips) or "zatím žádná data")
        gs = self.pulse.store.gem_status
        if time.time() - getattr(self, "_spend_ts", 0) > 15:
            self._spend_ts = time.time()
            try:
                from coinscan import llm
                self._spend = llm.spent_today()
            except Exception:  # noqa: BLE001
                self._spend = (0, 0)
        usd, calls = getattr(self, "_spend", (0, 0))
        self.gem_status.setText((f"💎 {gs}" if gs else "") + (f"   ·   AI dnes ${usd:.2f} ({calls}×)" if calls else ""))

    def open_env(self):
        env_path = ROOT / ".env"
        if not env_path.exists():
            env_path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
            QMessageBox.information(self, "CoinScan", "Vytvořil jsem .env ze šablony – doplň klíče a ulož.")
        _open_path(env_path)

    def on_cfg_saved(self, cfg: dict):
        fresh = load_config()
        self.settings.reload()
        self.pulse.update_cfg(fresh)
        self.settings.cfg = fresh
        self.risk.cfg = fresh
        msg = "Nastavení uloženo – Pulse použije hned"
        if self.thread and self.thread.is_alive():
            msg += ", scanner po restartu (Stop → Start)"
        self._queue_log("INFO", msg + ".")

    def closeEvent(self, e):
        self._flush_log()
        self.pulse.shutdown()
        if self.engine:
            self.engine.stop()
        for p in list(self.popups.popups):
            p.close()
        super().closeEvent(e)


def _open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(path)])


STYLE = """
QWidget { background:#0f1117; color:#e5e7eb; font-size:10pt; }
QPushButton { background:#1f2937; border:1px solid #374151; padding:6px 14px; border-radius:6px; }
QPushButton:hover { background:#374151; }
QPushButton#start { background:#15803d; border-color:#16a34a; font-weight:bold; }
QPushButton#stop { background:#b91c1c; border-color:#dc2626; font-weight:bold; }
QPushButton#tf { padding:3px 4px; font-size:9pt; }
QPushButton#tf:checked { background:#2563eb; border-color:#3b82f6; font-weight:bold; }
QTabBar::tab { background:#1f2937; padding:8px 16px; margin-right:2px; }
QTabBar::tab:selected { background:#374151; }
QHeaderView::section { background:#1f2937; padding:4px; border:0; }
QTableView { gridline-color:#1f2937; selection-background-color:#1e3a8a; }
QPlainTextEdit, QTextEdit, QTextBrowser { background:#0b0d12; border:1px solid #1f2937; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit { background:#1f2937; padding:4px; border:1px solid #374151; border-radius:4px; }
QGroupBox { border:1px solid #1f2937; border-radius:6px; margin-top:14px; padding-top:6px; }
QGroupBox::title { subcontrol-origin: margin; left:8px; color:#93c5fd; font-weight:bold; }
QListView { background:#0b0d12; border:1px solid #1f2937; }
QScrollArea { border:0; }
QToolTip { background:#111827; color:#e5e7eb; border:1px solid #374151; }
"""


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
