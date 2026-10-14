"""Záložka Nastavení – presety + editor všech filtrů."""
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from coinscan import presets
from coinscan.config import load_config, save_config

from .widgets import ConfigForm

RISKS = ["LOW", "MEDIUM", "HIGH"]


def _col(col: str, title: str):
    p = f"pulse.columns.{col}"
    return (title, [
        (f"{p}.min_mcap", "int", (0, 100_000_000), "Min MCap ($)", ""),
        (f"{p}.max_mcap", "int", (0, 1_000_000_000), "Max MCap ($, 0 = bez)", ""),
        (f"{p}.max_age_min", "int", (0, 100_000), "Max stáří (min, 0 = bez)", ""),
        (f"{p}.min_progress", "int", (0, 100), "Min bonding progress (%)", ""),
        (f"{p}.require_socials", "bool", None, "Jen se socials", ""),
        (f"{p}.min_replies", "int", (0, 10_000), "Min komentářů", ""),
        (f"{p}.max_risk", "choice", RISKS, "Max risk", "Coiny s horším ratingem se skryjí"),
        (f"{p}.hide_keywords", "text", None, "Skrýt slova", "čárkou oddělené, např. test, rug, scam"),
    ])


SCHEMA = [
    ("Scanner", [
        ("scanner.scan_interval_sec", "int", (10, 3600), "Interval scanu (s)", ""),
        ("scanner.sources", "multi", ["token_profiles", "token_boosts", "pumpfun"], "Zdroje", ""),
        ("scanner.pumpfun_min_progress", "int", (0, 100), "pump.fun min progress (%)",
         "Ze zdroje pumpfun brát coiny od tohoto % bonding curve (100 = jen migrované)"),
    ]),
    ("1) Základní filtry (DexScreener)", [
        ("basic_filters.max_age_minutes", "int", (1, 100_000), "Max stáří páru (min)", ""),
        ("basic_filters.min_liquidity_usd", "int", (0, 100_000_000), "Min likvidita ($)", ""),
        ("basic_filters.max_liquidity_usd", "int", (0, 1_000_000_000), "Max likvidita ($)", ""),
        ("basic_filters.min_mcap_usd", "int", (0, 1_000_000_000), "Min MCap ($, 0 = vypnuto)", ""),
        ("basic_filters.max_mcap_usd", "int", (0, 1_000_000_000), "Max MCap ($)", ""),
        ("basic_filters.min_volume_h1_usd", "int", (0, 1_000_000_000), "Min objem 1h ($)", ""),
        ("basic_filters.min_buys_m5", "int", (0, 10_000), "Min nákupů za 5 min", ""),
        ("basic_filters.min_buy_sell_ratio_h1", "float", (0, 10, 0.05), "Min buy/sell 1h", ""),
        ("basic_filters.require_socials", "bool", None, "Vyžadovat socials", ""),
    ]),
    ("Stale filtr (staré mrtvé coiny ignorovat)", [
        ("stale_filter.enabled", "bool", None, "Zapnuto", ""),
        ("stale_filter.older_than_hours", "int", (1, 24 * 60), "Platí pro coiny starší než (h)", ""),
        ("stale_filter.min_mcap_per_day_usd", "int", (0, 100_000_000), "Min MC za každý den stáří ($)",
         "Např. 100k: 2denní coin potřebuje ≥ 200k MC, jinak ho scanner vůbec nebere"),
    ]),
    ("💎 Gem detektor (auto AI + popupy)", [
        ("gems.enabled", "bool", None, "Zapnuto", ""),
        ("gems.auto_ai", "bool", None, "Automatická AI analýza", "Nejlepší kandidáty pošle na AI report se strategií"),
        ("gems.watch_score", "int", (0, 100), "WATCH od gem skóre", "Od tohohle Jev skóre; pod tím žádná AI (jen tlačítkem)"),
        ("gems.buy_score", "int", (0, 100), "BUY od gem skóre", "Od tohohle + AI report (free, placený pojistka)"),
        ("gems.report_min_jev", "int", (0, 100), "Report jen od Jev skóre", ""),
        ("gems.max_jev_per_min", "int", (1, 300), "Max Jev dotazů / min", ""),
        ("gems.ai_parallel", "int", (1, 6), "AI analýz najednou", "Víc = menší zpoždění u gemů"),
        ("gems.from_scanner", "bool", None, "Posílat i coiny ze scanneru", "I ty s nízkým MC, co filtry vyhodí"),
        ("gems.track_fast_sec", "int", (15, 3600), "BUY/WATCH aktualizovat po (s)", ""),
        ("gems.slop_stop_min", "int", (1, 1440), "SKIP přestat sledovat po (min)", ""),
        ("gems.ai_retry_min", "int", (1, 120), "Bez AI zkusit znovu po (min)", ""),
        ("gems.popup_verdicts", "multi", ["BUY", "WATCH"], "Popup při verdiktu", ""),
        ("gems.min_confidence", "float", (0, 1, 0.05), "Min AI confidence pro popup", ""),
        ("gems.popup_score_without_ai", "int", (0, 101), "Popup bez AI od skóre", "101 = nikdy"),
        ("gems.min_mcap", "int", (0, 100_000_000), "Min MC ($)", ""),
        ("gems.max_mcap", "int", (0, 1_000_000_000), "Max MC ($)", ""),
        ("gems.sweet_mcap_min", "int", (0, 100_000_000), "Ideální MC od ($)", "Bonus ve skóre – nejvíc prostoru k 20×"),
        ("gems.sweet_mcap_max", "int", (0, 100_000_000), "Ideální MC do ($)", ""),
        ("gems.popup_sound", "bool", None, "Zvuk u popupu", ""),
        ("gems.popup_seconds", "int", (0, 3600), "Popup zmizí po (s, 0 = nikdy)", ""),
    ]),
    ("⚖ Váhy gem skóre (1.0 = normál)", [
        (f"gems.weights.{k}", "float", (0, 3, 0.1), label, "") for k, label in (
            ("momentum", "Momentum (růst MC)"), ("activity", "Aktivita (obchody)"), ("volume", "Zrychlení objemu"),
            ("bonding", "Rychlost bonding"), ("ath", "Blízko ATH"), ("community", "Komunita"),
            ("room", "Prostor k růstu"), ("safety", "Bezpečnost"))
    ]),
    ("👁 Watch stavy (SHIT → LONG / SHORT → GEM)", [
        ("watch.jev_good", "int", (0, 100), "Jev pro postup na SHORT", "Pod tím: SHIT → LONG, LONG → archiv"),
        ("watch.long_max", "int", (10, 2000), "Long watch – top N coinů", ""),
        ("watch.long_max_min", "int", (1, 240), "Long watch – max minut", ""),
        ("watch.long_check_sec", "int", (5, 300), "Long watch – kontrola po (s)", ""),
        ("watch.long_improve_pts", "int", (1, 50), "Long: zlepšení o (bodů) → Jev", ""),
        ("watch.long_drop_pts", "int", (1, 60), "Long: zhoršení o (bodů) → archiv", ""),
        ("watch.long_mc_drop_pct", "int", (5, 95), "Long: MC −% z vrcholu → archiv", ""),
        ("watch.confirm_checks", "int", (1, 10), "Zhoršení potvrdit (× po sobě)", ""),
        ("watch.short_cycle_sec", "int", (5, 600), "Short watch – cyklus (s)", ""),
        ("watch.short_jev_every", "int", (1, 50), "Short: Jev každý N-tý cyklus", ""),
        ("watch.short_drop_pts", "int", (1, 60), "Short: zhoršení o (bodů) → long", ""),
        ("watch.short_jev_drop", "int", (1, 100), "Short: Jev −X z maxima → long", ""),
        ("watch.short_max_min", "int", (1, 600), "Short: max minut bez BUY → long", ""),
        ("watch.gem_exit_drop_pct", "int", (5, 95), "Gem: MC −% z vrcholu → short", ""),
        ("watch.jev_low_share", "int", (0, 100), "SHIT/LONG smí % Jev limitu", "Zbytek je rezerva pro SHORT a GEM"),
    ]),
    ("🔄 Hluboké sledování BUY gemů (i v záložce Gemy)", [
        ("buy_tracking.enabled", "bool", None, "Zapnuto", ""),
        ("buy_tracking.cycle_sec", "int", (10, 600), "1 cyklus (s)", "Aktualizace ceny bez AI"),
        ("buy_tracking.deep_cycles", "int", (1, 2000), "Hluboce sledovat (cyklů)", ""),
        ("buy_tracking.jev_every_cycles", "int", (0, 100), "Jev každý N-tý cyklus (0 = vypnuto)", ""),
        ("buy_tracking.jev_max_updates", "int", (0, 1000), "Max Jev na gem", ""),
        ("buy_tracking.ai_updates", "bool", None, "AI updaty (trend, držet/prodat)", ""),
        ("buy_tracking.ai_every_cycles", "int", (1, 500), "AI každý N-tý cyklus", ""),
        ("buy_tracking.ai_max_updates", "int", (0, 100), "Max AI updatů na gem", ""),
        ("buy_tracking.ai_allow_paid", "bool", None, "Placená AI jako pojistka pro updaty", ""),
        ("buy_tracking.popup_actions", "multi", ["SELL", "TAKE_PROFIT", "ADD", "HOLD"], "Popup při", ""),
        ("gems.nonbuy_archive_min", "int", (1, 1440), "AI SKIP do archivu po (min)", ""),
    ]),
    ("🏁 Runneři (zpětná kontrola)", [
        ("runners.enabled", "bool", None, "Zapnuto", ""),
        ("runners.interval_min", "int", (2, 240), "Skenovat každých (min)", ""),
        ("runners.min_mcap", "int", (10_000, 100_000_000), "Runner = MC/ATH aspoň ($)", ""),
        ("runners.max_age_hours", "int", (1, 168), "Max stáří runneru (h)", ""),
        ("runners.ai_postmortem", "bool", None, "AI rozbor + doporučení", "1 dotaz na scan s novými runnery (~$0.05)"),
    ]),
    ("🤖 Bot filtr (fake coiny, zdarma)", [
        ("bot_filter.enabled", "bool", None, "Zapnuto", "Schody, pila, stejné objemy, hodinky, wash trading"),
        ("bot_filter.hide", "bool", None, "Boty v Pulse schovat", "Vypnuto = jen červeně označit"),
        ("bot_filter.hide_unchecked", "bool", None, "Ukázat coin až po kontrole", ""),
        ("bot_filter.sensitivity", "choice", ["nízká", "střední", "vysoká"], "Citlivost",
         "Vysoká chytí víc botů, ale občas i normální coin"),
        ("bot_filter.columns", "multi", ["new", "soon", "migrated"], "Sloupce", ""),
        ("bot_filter.min_age_sec", "int", (10, 600), "Kontrolovat od stáří (s)", ""),
        ("bot_filter.window_sec", "int", (120, 1800), "Analyzovat posledních (s)", ""),
        ("bot_filter.recheck_sec", "int", (20, 1800), "Opakovat kontrolu po (s)", ""),
        ("bot_filter.checks_per_sec", "float", (0.2, 10, 0.2), "Max kontrol za s", ""),
        ("bot_filter.sticky", "bool", None, "Jednou bot = pořád bot", ""),
        ("bot_filter.scanner", "bool", None, "Použít i ve scanneru", "Vyřadí boty dřív, než se zavolá RugCheck/AI"),
    ]),
    ("2) Bezpečnost (RugCheck)", [
        ("safety_filters.enabled", "bool", None, "Zapnuto", ""),
        ("safety_filters.max_rugcheck_score", "int", (0, 100), "Max RugCheck skóre", "0–100, víc = horší"),
        ("safety_filters.reject_mint_authority", "bool", None, "Zamítnout mint authority", ""),
        ("safety_filters.reject_freeze_authority", "bool", None, "Zamítnout freeze authority", ""),
        ("safety_filters.max_top10_holders_pct", "int", (0, 100), "Max top10 holderů (%)", ""),
        ("safety_filters.reject_danger_risks", "bool", None, "Zamítnout 'danger' rizika", ""),
        ("safety_filters.require_rugcheck", "bool", None, "Bez RugChecku zamítnout",
         "Vypnuto = když RugCheck nejde, coin pokračuje k AI"),
    ]),
    ("🤖 AI – free hlavní, placený pojistka, Jev skóre", [
        ("llm.enabled", "bool", None, "AI zapnutá", ""),
        ("llm.max_free_per_hour", "int", (1, 500), "Free max / hod", "Nad limit (nebo při chybě) přeskočí na placený"),
        ("llm.free_timeout", "int", (10, 180), "Free timeout (s)", ""),
        ("llm.use_paid", "bool", None, "Placená pojistka", "Jen když free nejde. Modely jen z .env (OPENROUTER_PAID_MODELS + OPENROUTER_API_KEY_PAID)"),
        ("llm.report_web_search", "bool", None, "Placený report hledá na webu/X", "+ ~$0.02"),
        ("llm.max_usd_per_day", "float", (0, 100, 0.5), "Max $ za den (placené + Jev)", ""),
        ("llm.jev_max_scam", "float", (0, 1, 0.05), "Jev: max pravděpodobnost scamu", "pro 'pass' ve scanneru"),
        ("llm.jev_min_potential", "float", (0, 3, 0.1), "Jev: min potenciál (0–3)", ""),
        ("llm.require_jev_pass", "bool", None, "Scanner: Jev musí pustit", ""),
    ]),
    ("3) AI + vyhodnocení", [
        ("llm.enabled", "bool", None, "AI kontrola zapnutá", ""),
        ("llm.min_confidence", "float", (0, 1, 0.05), "Min AI confidence", ""),
        ("tracking.target_multiple", "float", (1.1, 100, 0.1), "Cíl (hit = peak ≥ X×)", ""),
        ("tracking.duration_hours", "int", (1, 168), "Sledovat (h)", ""),
        ("tracking.interval_sec", "int", (30, 3600), "Aktualizace ceny (s)", ""),
        ("tracking.track_rejected", "bool", None, "Sledovat i zamítnuté", ""),
        ("risk.low_max", "int", (0, 100), "LOW risk do skóre", ""),
        ("risk.medium_max", "int", (0, 100), "MEDIUM risk do skóre", ""),
    ]),
    ("Pulse – obecné", [
        ("pulse.poll_sec", "int", (1, 60), "Obnovení pump.fun (s)", "2 = 2 dotazy každé 2 s"),
        ("pulse.websocket", "bool", None, "PumpPortal websocket", "Nové coiny a migrace okamžitě"),
        ("pulse.default_timeframe", "choice", ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "1h"],
         "Výchozí timeframe grafu", ""),
        ("pulse.keep_hours", "int", (1, 72), "Držet coiny (h)", ""),
        ("pulse.max_coins", "int", (200, 10000), "Max coinů v paměti", "Víc = pomalejší GUI"),
        ("pulse.soon_progress", "int", (1, 99), "'Soon' od progressu (%)", ""),
        ("pulse.auto_rugcheck", "multi", ["new", "soon", "migrated"], "Auto RugCheck", ""),
        ("pulse.rugcheck_per_min", "int", (0, 60), "Auto RugCheck / min",
         "Kolik coinů za minutu smí Pulse kontrolovat. Bez klíče drž ~10, ať zbyde limit pro scanner. 0 = vypnuto"),
        ("alerts.discord_enabled", "bool", None, "Discord alerty", ""),
        ("alerts.max_risk_label", "choice", RISKS, "Alert max risk", ""),
    ]),
    _col("new", "Pulse – sloupec NEW"),
    _col("soon", "Pulse – sloupec SOON MIGRATING"),
    _col("migrated", "Pulse – sloupec MIGRATED"),
]


class _TestSig(QObject):
    done = Signal(str)


class SettingsView(QWidget):
    saved = Signal(dict)

    def __init__(self):
        super().__init__()
        self._test_sig = _TestSig()
        self.cfg = load_config()

        # presety
        self.preset_box = QComboBox()
        self.preset_desc = QLabel()
        self.preset_desc.setWordWrap(True)
        self.preset_desc.setStyleSheet("color:#9ca3af")
        self.preset_box.currentTextChanged.connect(self._show_desc)
        b_apply = QPushButton("Načíst preset")
        b_apply.clicked.connect(self.apply_preset)
        b_saveas = QPushButton("Uložit jako vlastní…")
        b_saveas.clicked.connect(self.save_as)
        b_del = QPushButton("Smazat vlastní")
        b_del.clicked.connect(self.delete_preset)
        top = QHBoxLayout()
        top.addWidget(QLabel("Preset:"))
        top.addWidget(self.preset_box, 1)
        for b in (b_apply, b_saveas, b_del):
            top.addWidget(b)

        self.form = ConfigForm(SCHEMA)
        self.form.changed.connect(lambda: self.dirty.setText("● neuložené změny"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)

        self.dirty = QLabel("")
        self.dirty.setStyleSheet("color:#eab308")
        b_save = QPushButton("💾  Uložit nastavení")
        b_save.setObjectName("start")
        b_save.clicked.connect(self.save)
        self.b_test = QPushButton("🤖 Test AI")
        self.b_test.setToolTip("Pošle krátký dotaz na OpenRouter a ukáže přesnou chybu, když něco nejde")
        self.b_test.clicked.connect(self.test_ai)
        self._test_sig.done.connect(self._test_done)
        b_revert = QPushButton("Vrátit")
        b_revert.clicked.connect(self.reload)
        bottom = QHBoxLayout()
        bottom.addWidget(self.dirty)
        bottom.addStretch()
        bottom.addWidget(self.b_test)
        bottom.addWidget(b_revert)
        bottom.addWidget(b_save)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.preset_desc)
        lay.addWidget(scroll, 1)
        lay.addLayout(bottom)
        self.reload()

    def _fill_presets(self):
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        custom = presets.custom_presets()
        for name in presets.BUILTIN:
            self.preset_box.addItem(name)
        for name in custom:
            self.preset_box.addItem(name)
        self.preset_box.setCurrentText(self.cfg.get("active_preset", ""))
        self.preset_box.blockSignals(False)
        self._show_desc(self.preset_box.currentText())

    def _show_desc(self, name):
        p = presets.all_presets().get(name, {})
        tag = "⭐ doporučený" if name == "Migration play" else ("vestavěný" if name in presets.BUILTIN else "vlastní")
        active = "  (aktivní)" if name == self.cfg.get("active_preset") else ""
        self.preset_desc.setText(f"<b>{name}</b> – {tag}{active}<br>{p.get('_popis', '')}")

    def reload(self):
        self.cfg = load_config()
        self.form.load(self.cfg)
        self._fill_presets()
        self.dirty.setText("")

    def apply_preset(self):
        name = self.preset_box.currentText()
        self.form.dump_into(self.cfg)
        self.cfg = presets.apply(self.cfg, name)
        self.form.load(self.cfg)
        self.dirty.setText(f"● preset '{name}' načten – ulož pro použití")
        self._show_desc(name)

    def save_as(self):
        name, ok = QInputDialog.getText(self, "Vlastní preset", "Název presetu:")
        if not ok or not name.strip():
            return
        popis, _ = QInputDialog.getText(self, "Vlastní preset", "Krátký popis (volitelné):")
        self.cfg = load_config()
        self.form.dump_into(self.cfg)
        safe = presets.save_custom(name, self.cfg, popis)
        self.cfg["active_preset"] = safe
        save_config(self.cfg)
        self._fill_presets()
        self.dirty.setText("")
        self.saved.emit(self.cfg)

    def delete_preset(self):
        name = self.preset_box.currentText()
        if name in presets.BUILTIN:
            QMessageBox.information(self, "Preset", "Vestavěné presety smazat nejdou.")
            return
        if presets.delete_custom(name):
            self._fill_presets()

    def save(self):
        fresh = load_config()  # ať nepřepíšeme změny z jiné záložky (Risk management)
        self.form.dump_into(fresh)
        fresh["active_preset"] = self.cfg.get("active_preset", fresh.get("active_preset"))
        self.cfg = fresh
        save_config(self.cfg)
        self.dirty.setText("✓ uloženo")
        self._show_desc(self.preset_box.currentText())
        self.saved.emit(self.cfg)

    def test_ai(self):
        from coinscan import llm
        from coinscan.config import load_config as _lc
        _lc()  # načte .env znovu
        self.b_test.setEnabled(False)
        self.dirty.setText("⏳ testuji free (hlavní), Jev a placený (pojistka)…")

        def work():
            try:
                msg = llm.test(_lc())
            except Exception as e:  # noqa: BLE001
                msg = f"✗ AI nejde: {e}"
            self._test_sig.done.emit(msg)

        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, msg):
        self.b_test.setEnabled(True)
        self.dirty.setText(msg)
        col = "#22c55e" if "✗" not in msg else "#eab308" if "✓" in msg else "#ef4444"
        self.dirty.setStyleSheet(f"color:{col}")
        self.dirty.setWordWrap(True)
