"""Záložka Úspěšnost – fungují moje filtry? Lidskou řečí, s kartami a závěry."""
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from coinscan import gems, stats
from coinscan.config import load_config

MIN_N = 20      # pod tímhle počtem vyhodnocených coinů jsou čísla jen náhoda


def pct(x):
    return "–" if x is None else f"{x * 100:.0f} %"


def card(title, big, sub, color="#e5e7eb"):
    return (f"<td style='background:#111827;border:1px solid #1f2937;padding:10px 14px;width:25%'>"
            f"<div style='color:#9ca3af'>{title}</div>"
            f"<div style='font-size:20pt;font-weight:bold;color:{color}'>{big}</div>"
            f"<div style='color:#9ca3af'>{sub}</div></td>")


class _Sig(QObject):
    loaded = Signal(object, object)
    exported = Signal(str)


class StatsView(QWidget):
    def __init__(self):
        super().__init__()
        self.sig = _Sig()
        self.sig.loaded.connect(self._fill)
        self._busy = False
        self.label = QLabel("Načítám…")
        self.label.setTextFormat(Qt.RichText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignTop)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.label)
        # export logu / reportu
        bar = QHBoxLayout()
        bar.addWidget(QLabel("⬇ Stáhnout:"))
        self.period = QComboBox()
        for label, h in (("posledních 24 h", 24), ("posledních 6 h", 6), ("7 dní", 168), ("vše", 100000)):
            self.period.addItem(label, h)
        bar.addWidget(self.period)
        for text, kind, tip in (("CSV gemů", "csv", "Všechny gemy (i archiv) do Excelu – skóre, Jev, AI, výsledky"),
                                ("Report (bez AI)", "md", "Markdown report: souhrn, Jev jako prediktor, BUY gemy s vývojem, archiv"),
                                ("Report + shrnutí free AI", "md_ai", "Stejný report + krátké shrnutí a tipy od FREE AI (nic neplatíš)")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=kind: self.export(k))
            bar.addWidget(b)
        self.export_msg = QLabel()
        self.export_msg.setStyleSheet("color:#22c55e")
        bar.addWidget(self.export_msg, 1)
        self.sig.exported.connect(self.export_msg.setText)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(sc)

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        target = load_config().get("tracking", {}).get("target_multiple", 2.0)

        def work():
            try:
                d, g = stats.dashboard(target), gems.summary()
            except Exception as e:  # noqa: BLE001
                d, g = {"error": str(e)}, None
            self.sig.loaded.emit(d, g)

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, d, g):
        self._busy = False
        if "error" in d:
            self.label.setText(f"Chyba: {d['error']}")
            return
        t = d["target"]
        P, R, A = d["passed"], d["rejected"], d["all"]
        h = [f"<h2>Fungují moje filtry?</h2>"
             f"<p style='color:#9ca3af'>Appka si pamatuje každý coin, který scanner viděl – i ty, co vyhodil – "
             f"a 24 h sleduje, kam až vyletí. <b>Hit</b> = coin od nálezu dosáhl aspoň <b>{t:g}×</b>. "
             f"Dobré filtry = coiny, co <b>prošly</b>, mají vyšší hit rate a nižší rug rate než ty <b>zamítnuté</b>.</p>"]

        # --- karty
        h.append("<table cellspacing='8' width='100%'><tr>")
        h.append(card("Sledováno coinů", A["total"], f"vyhodnoceno {A['done']} (zbytek ještě běží 24 h)"))
        h.append(card("✅ Prošly filtry", P["total"], f"hit rate {pct(P['hit_rate'])} · rug {pct(P['rug_rate'])}",
                      "#22c55e"))
        h.append(card("❌ Zamítnuté", R["total"], f"hit rate {pct(R['hit_rate'])} · rug {pct(R['rug_rate'])}"))
        if g:
            h.append(card("💎 Gem alerty", g["total"],
                          f"2×+: {g['tiers'][2]} · 5×+: {g['tiers'][5]} · 10×+: {g['tiers'][10]} · 20×+: {g['tiers'][20]}",
                          "#a855f7"))
        h.append("</tr></table>")

        # --- závěr
        h.append("<h3>📌 Závěr</h3><p>")
        if P["done"] < MIN_N:
            h.append(f"Zatím <b>málo dat</b> – vyhodnoceno jen {P['done']} coinů, které prošly filtry "
                     f"(potřeba aspoň {MIN_N}). Nech scanner běžet aspoň den, pak čísla začnou dávat smysl.")
        elif R["hit_rate"] is not None and P["hit_rate"] is not None:
            if P["hit_rate"] > R["hit_rate"] * 1.3:
                h.append(f"✅ <b>Filtry fungují.</b> Coiny, které prošly, dosáhly {t:g}× v {pct(P['hit_rate'])} případů, "
                         f"zamítnuté jen v {pct(R['hit_rate'])}.")
            elif P["hit_rate"] < R["hit_rate"]:
                h.append(f"⚠ <b>Filtry spíš škodí</b> – zamítnuté coiny mají lepší výsledky ({pct(R['hit_rate'])}) "
                         f"než ty, co prošly ({pct(P['hit_rate'])}). Koukni níž, který filtr vyhazuje vítěze.")
            else:
                h.append("😐 Filtry zatím nedělají velký rozdíl mezi dobrými a špatnými coiny.")
            if P["rug_rate"] is not None and R["rug_rate"] is not None and P["rug_rate"] < R["rug_rate"]:
                h.append(f"<br>🛡 Rugů mezi prošlými je méně ({pct(P['rug_rate'])} vs. {pct(R['rug_rate'])}) – "
                         f"bezpečnostní filtry chrání.")
        h.append("</p>")

        # --- který filtr vyhazuje vítěze
        h.append("<h3>🔍 Který filtr co vyhodil</h3>"
                 "<p style='color:#9ca3af'>Když nějaký důvod vyhazuje coiny, které pak často letí, "
                 "je filtr moc přísný – uprav ho ve Filtrech.</p>")
        h.append("<table cellpadding='5' cellspacing='0' width='100%' style='border-collapse:collapse'>"
                 "<tr style='background:#1f2937'><th align='left'>Důvod zamítnutí</th><th>Coinů</th>"
                 f"<th>Vyhodnoceno</th><th>Z toho {t:g}×+</th><th>Hit rate</th><th>Rug rate</th><th>Verdikt</th></tr>")
        base = R["hit_rate"] or 0
        for name, s in d["by_reason"][:25]:
            if s["done"] >= 5 and s["hit_rate"] is not None and s["hit_rate"] > max(base * 1.5, 0.15):
                verdict = "<span style='color:#eab308'>⚠ vyhazuje vítěze</span>"
            elif s["done"] >= 5 and s["rug_rate"] is not None and s["rug_rate"] > 0.4:
                verdict = "<span style='color:#22c55e'>✓ chrání před rugy</span>"
            elif s["done"] < 5:
                verdict = "<span style='color:#6b7280'>málo dat</span>"
            else:
                verdict = "✓ ok"
            h.append(f"<tr><td>{name}</td><td align='center'>{s['total']}</td><td align='center'>{s['done']}</td>"
                     f"<td align='center'>{s['hits']}</td><td align='center'>{pct(s['hit_rate'])}</td>"
                     f"<td align='center'>{pct(s['rug_rate'])}</td><td align='center'>{verdict}</td></tr>")
        h.append("</table>")

        # --- AI
        if d["by_verdict"]:
            h.append("<h3>🤖 Trefuje se AI?</h3><table cellpadding='5' width='100%'>"
                     "<tr style='background:#1f2937'><th align='left'>AI řekla</th><th>Coinů</th><th>Vyhodnoceno</th>"
                     f"<th>{t:g}×+</th><th>5×+</th><th>10×+</th><th>Rug</th><th>Průměrný max ×</th></tr>")
            for v, s in d["by_verdict"].items():
                ap = "–" if s["avg_peak"] is None else f"{s['avg_peak']:.2f}×"
                h.append(f"<tr><td><b>{v}</b></td><td align='center'>{s['total']}</td><td align='center'>{s['done']}</td>"
                         f"<td align='center'>{pct(s['hit_rate'])}</td><td align='center'>{s['x5']}</td>"
                         f"<td align='center'>{s['x10']}</td><td align='center'>{pct(s['rug_rate'])}</td>"
                         f"<td align='center'>{ap}</td></tr>")
            h.append("</table><p style='color:#9ca3af'>Když BUY má výrazně lepší čísla než SKIP, AI pomáhá.</p>")

        # --- gemy
        if g and g["total"]:
            h.append("<h3>💎 Gem alerty (popupy)</h3><p>"
                     f"Celkem {g['total']} gemů, AI řekla BUY u {g['buy']}. Od alertu vyletěly:<br>"
                     + " · ".join(f"<b>{x}×+</b>: {g['tiers'][x]} ({pct(g['tiers'][x] / g['total'])})" for x in (2, 5, 10, 20))
                     + "<br>Z BUY: "
                     + " · ".join(f"<b>{x}×+</b>: {g['buy_tiers'][x]}" for x in (2, 5, 10, 20))
                     + "<br><span style='color:#9ca3af'>Detail každého gemu je v záložce 💎 Gemy.</span></p>")
        self.label.setText("".join(h))

    def export(self, kind: str):
        import os
        from datetime import datetime

        from coinscan import export
        from coinscan.config import ROOT
        d = ROOT / "data" / "exports"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        ext, flt = ("csv", "CSV (*.csv)") if kind == "csv" else ("md", "Markdown (*.md)")
        path, _ = QFileDialog.getSaveFileName(self, "Uložit", str(d / f"coinscan_{kind}_{stamp}.{ext}"), flt)
        if not path:
            return
        h = self.period.currentData()
        self.export_msg.setText("⏳ exportuji…" + (" (free AI píše shrnutí, až 90 s)" if kind == "md_ai" else ""))

        def work():
            try:
                if kind == "csv":
                    n = export.gems_csv(path, h)
                    msg = f"✓ uloženo {n} gemů → {path}"
                else:
                    note = export.report_md(path, h, with_ai=kind == "md_ai", cfg=load_config())
                    msg = f"✓ report uložen → {path}" + (f" ({note})" if note else "")
                try:
                    os.startfile(os.path.dirname(path))  # noqa: S606 – otevře složku (Windows)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                msg = f"✗ export selhal: {e}"
            self.sig.exported.emit(msg)

        threading.Thread(target=work, daemon=True).start()
