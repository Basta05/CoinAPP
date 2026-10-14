"""Záložka Risk management – pravidla + kalkulačka pozice."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from coinscan import riskmgmt
from coinscan.config import load_config, save_config

from .widgets import ConfigForm

SCHEMA = [
    ("Pravidla", [
        ("risk_mgmt.bankroll_sol", "float", (0.01, 100000, 0.1), "Bankroll (SOL)", "Kolik máš vyhrazeno na memecoiny"),
        ("risk_mgmt.risk_per_trade_pct", "float", (0.1, 50, 0.1), "Riziko na trade (%)", "Kolik % bankrollu ztratíš, když trefí SL"),
        ("risk_mgmt.stop_loss_pct", "int", (1, 99), "Stop-loss (%)", ""),
        ("risk_mgmt.max_position_pct", "float", (0.5, 100, 0.5), "Max pozice (% bankrollu)", ""),
        ("risk_mgmt.max_open_positions", "int", (1, 50), "Max otevřených pozic", ""),
        ("risk_mgmt.daily_loss_limit_pct", "float", (1, 100, 1), "Denní stop (% bankrollu)", "Po dosažení na dnešek konec"),
    ]),
    ("Násobič pozice podle risku", [
        ("risk_mgmt.label_multiplier.LOW", "float", (0, 2, 0.05), "LOW", ""),
        ("risk_mgmt.label_multiplier.MEDIUM", "float", (0, 2, 0.05), "MEDIUM", ""),
        ("risk_mgmt.label_multiplier.HIGH", "float", (0, 2, 0.05), "HIGH", ""),
    ]),
]

RULES = """<b>Základní pravidla (memecoiny)</b><ul>
<li>Pozice počítej z toho, kolik chceš <b>ztratit</b>, ne kolik chceš vydělat.</li>
<li>Na 2× vytáhni aspoň vstup – zbytek je „free ride“.</li>
<li>SL na DEXu u memecoinů často proklouzne (rug = −90 %+). Proto malé pozice a násobič pro HIGH risk.</li>
<li>Po denním stopu přestaň – revenge trading je nejrychlejší cesta k nule.</li>
<li>Nikdy nekupuj coin s aktivní mint/freeze authority.</li></ul>"""


class RiskView(QWidget):
    saved = Signal(dict)

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.form = ConfigForm(SCHEMA)
        self.form.changed.connect(self.recalc)

        self.tp_edit = QLineEdit()
        self.tp_edit.setToolTip("Formát: násobek:procento, oddělené čárkou. Např. 2:50, 4:25")
        self.tp_edit.textChanged.connect(self.recalc)
        tp_box = QGroupBox("Take-profit plán")
        f = QFormLayout(tp_box)
        f.addRow("TP (násobek:% k prodeji)", self.tp_edit)

        calc = QGroupBox("Kalkulačka")
        cf = QFormLayout(calc)
        self.c_label = QComboBox()
        self.c_label.addItems(["LOW", "MEDIUM", "HIGH", "?"])
        self.c_label.currentTextChanged.connect(self.recalc)
        self.c_mcap = QDoubleSpinBox()
        self.c_mcap.setRange(0, 1e10)
        self.c_mcap.setDecimals(0)
        self.c_mcap.setSingleStep(1000)
        self.c_mcap.setValue(50000)
        self.c_mcap.valueChanged.connect(self.recalc)
        cf.addRow("Risk rating coinu", self.c_label)
        cf.addRow("Vstupní MCap ($)", self.c_mcap)
        self.out = QLabel()
        self.out.setWordWrap(True)
        cf.addRow(self.out)

        b_save = QPushButton("💾  Uložit")
        b_save.setObjectName("start")
        b_save.clicked.connect(self.save)
        self.status = QLabel()

        left = QVBoxLayout()
        left.addWidget(self.form)
        left.addWidget(tp_box)
        row = QHBoxLayout()
        row.addWidget(self.status)
        row.addStretch()
        row.addWidget(b_save)
        left.addLayout(row)
        left.addStretch()
        right = QVBoxLayout()
        right.addWidget(calc)
        rules = QLabel(RULES)
        rules.setWordWrap(True)
        right.addWidget(rules)
        right.addStretch()
        lay = QHBoxLayout(self)
        lay.addLayout(left, 3)
        lay.addLayout(right, 2)
        self.reload()

    def reload(self):
        self.cfg = load_config()
        self.form.load(self.cfg)
        tps = self.cfg.get("risk_mgmt", {}).get("take_profits") or []
        self.tp_edit.setText(", ".join(f"{x:g}:{p:g}" for x, p in tps))
        self.recalc()

    def _current(self, fresh: bool = False) -> dict:
        import copy
        cfg = load_config() if fresh else copy.deepcopy(self.cfg)
        cfg["risk_mgmt"] = dict(cfg.get("risk_mgmt", {}))
        self.form.dump_into(cfg)
        tps = []
        for part in self.tp_edit.text().split(","):
            try:
                x, p = part.split(":")
                tps.append([float(x), float(p)])
            except ValueError:
                continue
        cfg["risk_mgmt"]["take_profits"] = tps
        return cfg

    def recalc(self):
        cfg = self._current()
        p = riskmgmt.plan(cfg, self.c_label.currentText(), self.c_mcap.value() or None)
        self.out.setText(riskmgmt.fmt_plan(p))

    def save(self):
        self.cfg = self._current(fresh=True)
        save_config(self.cfg)
        self.status.setText("✓ uloženo")
        self.saved.emit(self.cfg)
