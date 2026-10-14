"""Popup okno pro 💎 gem – vyskočí v pravém dolním rohu přes všechno, s reportem a odkazy."""
from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .widgets import fmt_age, fmt_usd

VCOL = {"BUY": "#22c55e", "WATCH": "#eab308", "SKIP": "#ef4444"}
SCOL = {"OK": "#22c55e", "RISKY": "#eab308", "DANGER": "#ef4444"}


def report_html(gem: dict, compact: bool = False) -> str:
    """Report o gemu – používá popup i záložka Gemy."""
    c, ai, plan = gem["coin"], gem.get("ai") or {}, gem.get("plan") or {}
    st = ai.get("strategy") or {}
    out = []
    up = gem.get("update")
    if up:
        col = {"HOLD": "#60a5fa", "ADD": "#22c55e", "TAKE_PROFIT": "#eab308", "SELL": "#ef4444"}.get(up["action"], "#e5e7eb")
        out.append(f"<span style='font-size:13pt;font-weight:bold;color:{col}'>🔄 {up['action']}</span> "
                   f"· trend {up.get('trend', '?')} · cyklus {gem.get('cycle')} · {gem.get('mult', 1):.2f}× od alertu")
        out.append(f"<b>{up.get('summary', '')}</b>")
        if up.get("levels"):
            out.append(f"<span style='color:#9ca3af'>{up['levels']}</span>")
        out.append(f"MC <b>{fmt_usd(c.get('mcap'))}</b>")
        return "<br><br>".join(out)
    if ai:
        out.append(f"<span style='font-size:13pt;font-weight:bold;color:{VCOL.get(ai.get('verdict'), '#9ca3af')}'>"
                   f"{ai.get('verdict')}</span> &nbsp;jistota {ai.get('confidence', 0) * 100:.0f} % · "
                   f"bezpečnost <b style='color:{SCOL.get(ai.get('safety'), '#9ca3af')}'>{ai.get('safety', '?')}</b> · "
                   f"potenciál <b>{ai.get('potential', '?')}</b>")
        if ai.get("summary"):
            out.append(f"<b>{ai['summary']}</b>")
        if ai.get("thesis"):
            out.append(f"💡 {ai['thesis']}")
        if ai.get("narrative") and not compact:
            out.append(f"<span style='color:#9ca3af'>Narativ: {ai['narrative']}</span>")
    else:
        out.append("<b style='color:#eab308'>Bez AI</b> – AI teď nejde (limit/chyba), popup kvůli vysokému gem skóre")
    stats = [f"MC <b>{fmt_usd(c.get('mcap'))}</b>"]
    if c.get("ath_mcap"):
        stats.append(f"ATH {fmt_usd(c['ath_mcap'])}")
    stats.append("MIGROVÁNO" if c.get("complete") else f"bonding {c.get('progress', 0):.0f} %")
    if c.get("age_min") is not None:
        stats.append(f"stáří {fmt_age(c.get('age_min'))}")
    if gem.get("score") is not None:
        stats.append(f"💎 skóre {gem['score']}")
    jev = gem.get("jev") or (c.get("jev") if isinstance(c.get("jev"), dict) else None)
    if jev and jev.get("score") is not None:
        stats.append(f"🧮 Jev {jev['score']}")
    if c.get("risk_label"):
        stats.append(f"risk {c['risk_label']}")
    out.append(" · ".join(stats))
    scr = gem.get("screen") or {}
    if scr.get("tier") in ("cheap", "free"):
        out.append(f"<span style='color:#9ca3af'>✓ screen ({scr.get('model', '').split('/')[-1]}): "
                   f"{scr.get('reason', '')}</span>")
    if jev and jev.get("reason"):
        out.append(f"<span style='color:#9ca3af'>🧮 Jev: {jev['reason']}</span>")
    if ai and ai.get("model"):
        out.append(f"<span style='color:#6b7280'>report: {ai['model'].split('/')[-1]}"
                   + (" (placená pojistka + web – free nešel)" if ai.get("paid") else " (free)") + "</span>")
    if gem.get("why"):
        out.append("<span style='color:#93c5fd'>Proč: " + ", ".join(gem["why"][:6]) + "</span>")
    if st:
        s = ["<b>📋 Strategie</b>"]
        if st.get("entry"):
            s.append(f"Vstup: {st['entry']}" + (f" (max MC {fmt_usd(st['entry_max_mcap'])})"
                                                if isinstance(st.get("entry_max_mcap"), (int, float)) else ""))
        if isinstance(st.get("stop_loss_mcap"), (int, float)):
            s.append(f"Stop-loss: MC {fmt_usd(st['stop_loss_mcap'])}")
        if st.get("take_profits"):
            tps = st["take_profits"] if isinstance(st["take_profits"], list) else [st["take_profits"]]
            s.append("TP: " + " · ".join(str(t) for t in tps))
        if st.get("hold"):
            s.append(f"Držet: {st['hold']}")
        if st.get("invalidation"):
            s.append(f"Vzdát když: {st['invalidation']}")
        out.append("<br>".join(s))
    if plan:
        out.append(f"<b>💰 Tvoje pozice</b> {plan.get('size_sol', 0):.3f} SOL "
                   f"({plan.get('size_pct', 0):.1f} % bankrollu), SL −{plan.get('stop_loss_pct', 0):.0f} %")
    flags = ai.get("red_flags") or []
    if flags:
        out.append("<span style='color:#f87171'>⚠ " + " · ".join(str(f) for f in flags[:5]) + "</span>")
    return "<br><br>".join(out)


class GemPopup(QFrame):
    closed = Signal(object)
    focus_coin = Signal(str)

    def __init__(self, gem: dict, seconds: int = 90):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.gem = gem
        c = gem["coin"]
        self.setObjectName("popup")
        self.setStyleSheet("""
            #popup { background:#0f172a; border:2px solid %s; border-radius:10px; }
            QLabel { color:#e5e7eb; background:transparent; }
            QPushButton { background:#1f2937; color:#e5e7eb; border:1px solid #374151; padding:6px 6px;
                          border-radius:6px; font-weight:bold; }
            QPushButton:hover { background:#374151; }
            QPushButton#buy { background:#15803d; border-color:#16a34a; }
        """ % ("#eab308" if gem.get("update") else "#22c55e"))
        self.setFixedWidth(480)
        v = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel(("🔄 " if gem.get("update") else "💎 ") + f"<b style='font-size:15pt'>{c.get('symbol')}</b> "
                       f"<span style='color:#9ca3af'>{c.get('name', '')[:28]}</span>")
        title.setTextFormat(Qt.RichText)
        x = QPushButton("✕")
        x.setFixedWidth(34)
        x.clicked.connect(self.close)
        head.addWidget(title, 1)
        head.addWidget(x)
        v.addLayout(head)
        body = QLabel(report_html(gem, compact=True))
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setFixedWidth(452)          # pevná šířka -> správná výška zalomeného textu
        v.addWidget(body)
        ca = QLabel(f"<span style='color:#6b7280;font-size:8pt'>{c['mint']}</span>")
        ca.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(ca)
        row = QHBoxLayout()
        mint = c["mint"]
        for text, url, name in (("pump.fun", f"https://pump.fun/coin/{mint}", "buy"),
                                ("DexScreener", f"https://dexscreener.com/solana/{mint}", ""),
                                ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}", "")):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            row.addWidget(b)
        cp = QPushButton("📋 CA")
        cp.clicked.connect(lambda: (QGuiApplication.clipboard().setText(mint), cp.setText("✓")))
        row.addWidget(cp)
        show = QPushButton("📈 Graf")
        show.setToolTip("Otevřít v Pulse s grafem")
        show.clicked.connect(lambda: (self.focus_coin.emit(mint), self.close()))
        row.addWidget(show)
        v.addLayout(row)
        self._hover = False
        if seconds:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._auto_close)
            self._timer.start(seconds * 1000)

    def enterEvent(self, e):
        self._hover = True

    def leaveEvent(self, e):
        self._hover = False

    def _auto_close(self):
        if self._hover:
            self._timer.start(10_000)
        else:
            self.close()

    def closeEvent(self, e):
        self.closed.emit(self)
        super().closeEvent(e)


class PopupManager:
    """Skládá popupy nad sebe v pravém dolním rohu, max 4 najednou."""

    def __init__(self, main_window):
        self.win = main_window
        self.popups: list[GemPopup] = []

    def show(self, gem: dict, seconds: int, sound: bool, on_focus):
        while len(self.popups) >= 4:
            self.popups[0].close()
        p = GemPopup(gem, seconds)
        p.closed.connect(self._closed)
        p.focus_coin.connect(on_focus)
        self.popups.append(p)
        p.adjustSize()
        self._layout()
        p.show()
        p.raise_()
        if sound:
            QApplication.beep()
        QApplication.alert(self.win, 0)      # blikne ikona na liště

    def _closed(self, p):
        if p in self.popups:
            self.popups.remove(p)
        self._layout()

    def _layout(self):
        scr = QGuiApplication.primaryScreen().availableGeometry()
        y = scr.bottom() - 12
        for p in reversed(self.popups):
            p.adjustSize()
            y -= p.height()
            p.move(scr.right() - p.width() - 12, y)
            y -= 8
