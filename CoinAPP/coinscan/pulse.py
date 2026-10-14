"""Pulse – živý přehled pump.fun coinů rozdělený na New / Soon migrating / Migrated.

Zdroje: pump.fun polling (každé ~2 s, 2 dotazy paralelně) + PumpPortal websocket
(nové coiny a migrace okamžitě). Běží ve vláknech, GUI si bere lehký snapshot."""
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import dexscreener as dex
from . import candles, gems, llm, patterns, pumpfun, risk, riskmgmt, rugcheck, status
from .candles import ChartFeed
from .config import env
from .pumpportal import PumpPortal
from .watch import WatchManager

COLUMNS = ("new", "soon", "migrated")


class PulseStore:
    def __init__(self, cfg: dict, log=lambda lvl, msg: None):
        self.cfg = cfg
        self.log = log
        self.lock = threading.Lock()
        self.coins: dict[str, dict] = {}
        self.stop_event = threading.Event()
        self.selected: str | None = None
        self.last_error = ""
        self.last_update = 0.0
        self.poll_ms = 0.0
        self.sol_usd = 0.0
        self.version = 0                    # mění se při každé změně dat -> GUI nepřekresluje zbytečně
        self._threads: list[threading.Thread] = []
        self._pool = ThreadPoolExecutor(max_workers=3)
        self.chart = ChartFeed()
        self.watch = WatchManager(self)
        self.hist: dict[str, deque] = {}        # (ts, mcap) pro momentum v gem skóre
        self.on_gem = lambda gem: None          # GUI si nastaví – popup
        self.on_gem_update = lambda upd: None   # GUI – popup při SELL / TAKE_PROFIT
        self.buy_inflight: set[int] = set()
        self.buy_pool = ThreadPoolExecutor(max_workers=4)
        self.gem_analyzed: dict[str, tuple[float, int]] = {}
        self.ai_times: deque = deque()
        self.ai_cooldown_until = 0.0
        self.ai_inflight: set[str] = set()
        self.jev_inflight: set[str] = set()
        self.jev_times: deque = deque()
        self.gem_ids: dict[str, int] = {}
        self._seen_saved = 0.0
        self.ai_pool = ThreadPoolExecutor(max_workers=3)
        self.gem_status = ""
        self.ws = PumpPortal(self._on_ws_new, self._on_ws_migrate, log)

    # ---------- řízení ----------
    def start(self):
        if self._threads and any(t.is_alive() for t in self._threads):
            return
        self.stop_event.clear()
        self._threads = [threading.Thread(target=f, daemon=True)
                         for f in (self._poll_loop, self._rugcheck_loop, self._detail_loop, self._pattern_loop,
                                   self._gem_loop, self._buy_loop)]
        for t in self._threads:
            t.start()
        if self.cfg.get("pulse", {}).get("websocket", True):
            self.ws.start()

    def stop(self):
        self.stop_event.set()
        self.ws.stop()

    def shutdown(self):
        self.stop()
        self.chart.stop()

    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads) and not self.stop_event.is_set()

    def update_cfg(self, cfg: dict):
        with self.lock:
            self.cfg = cfg
            self.version += 1

    def select(self, mint: str, tf: str | None = None):
        self.selected = mint
        with self.lock:
            c = self.coins.get(mint)
        self.chart.set(mint, tf or self.chart.tf, c["created_ms"] if c else None)

    # ---------- snapshot pro GUI ----------
    def snapshot(self) -> tuple[int, dict[str, dict]]:
        with self.lock:
            # mělká kopie stačí – vlákna coin dict nahrazují, ne mění na místě
            return self.version, dict(self.coins)

    def column_of(self, c: dict) -> str:
        if c["complete"]:
            return "migrated"
        if c["progress"] >= self.cfg.get("pulse", {}).get("soon_progress", 60):
            return "soon"
        return "new"

    # ---------- websocket ----------
    def _on_ws_new(self, d: dict):
        now = time.time()
        c = {
            "mint": d["mint"], "name": d["name"], "symbol": d["symbol"], "image": "", "description": "",
            "creator": d["creator"], "created_ms": now * 1000, "age_min": 0.0, "complete": False,
            "progress": d["progress"], "mcap": d["market_cap_sol"] * self.sol_usd if self.sol_usd else 0.0,
            "ath_mcap": 0.0, "sol_in_curve": d["sol_in_curve"], "replies": 0, "last_trade_ms": now * 1000,
            "twitter": "", "telegram": "", "website": "", "is_live": False, "pool": "",
            "first_seen": now, "_ws": True,
        }
        c["ath_mcap"] = c["mcap"]
        with self.lock:
            if d["mint"] not in self.coins:
                self.coins[d["mint"]] = c
                self.version += 1

    def _on_ws_migrate(self, mint: str):
        with self.lock:
            old = self.coins.get(mint)
            if old and not old["complete"]:
                c = dict(old, complete=True, progress=100.0, migrated_at=time.time())
                self.coins[mint] = c
                self.version += 1
                if c["mcap"] > 20000:
                    self.log("INFO", f"🎓 {c['symbol']} migroval (mcap ${c['mcap']:,.0f})")

    # ---------- smyčky ----------
    def _poll_loop(self):
        backoff = 0
        while not self.stop_event.is_set():
            pc = self.cfg.get("pulse", {})
            t0 = time.time()
            try:
                f1 = self._pool.submit(pumpfun.list_coins, "created_timestamp", 50)
                f2 = self._pool.submit(pumpfun.list_coins, "last_trade_timestamp", 50)
                items = f1.result(timeout=15) + f2.result(timeout=15)
                self.poll_ms = (time.time() - t0) * 1000
                self._merge(items)
                self.last_error = ""
                self.last_update = time.time()
                status.ok("pump.fun", f"{self.poll_ms:.0f} ms", time.time() - t0)
                backoff = 0
            except Exception as e:  # noqa: BLE001
                backoff = min(60, (backoff or 5) * 2) if "429" in str(e) else 5
                if str(e) != self.last_error:
                    self.log("ERROR", f"Pulse: pump.fun – {e}")
                self.last_error = str(e)
                status.fail("pump.fun", f"{e} (pauza {backoff}s)")
            self._prune()
            # interval se počítá od začátku cyklu -> reálně ~2 s i když dotaz trvá
            wait = max(1.0, pc.get("poll_sec", 2)) - (time.time() - t0) + backoff
            self.stop_event.wait(max(0.2, wait))

    def _merge(self, items: list[dict]):
        now = time.time()
        max_age = self.cfg.get("pulse", {}).get("keep_hours", 6) * 60
        with self.lock:
            for c in items:
                if c["age_min"] > max_age and c["mint"] != self.selected:
                    continue
                if c.get("mcap") and c.get("_mcap_sol"):
                    self.sol_usd = c["mcap"] / c["_mcap_sol"]
                old = self.coins.get(c["mint"])
                if old:
                    if c["complete"] and not old["complete"]:
                        c["migrated_at"] = now
                        if c["mcap"] > 20000:
                            self.log("INFO", f"🎓 {c['symbol']} migroval (mcap ${c['mcap']:,.0f})")
                    for k in ("migrated_at", "first_seen", "rc", "risk_label", "risk_score", "dex", "pat", "pat_ts",
                              "gem", "gem_why", "ai", "screen", "injected", "jev", "jev_ts", "jev_error", "wstate", "wreason"):
                        if k in old and k not in c:
                            c[k] = old[k]
                    c["ath_mcap"] = max(c["ath_mcap"], old.get("ath_mcap", 0))
                else:
                    c["first_seen"] = now
                    if c["complete"]:
                        c["migrated_at"] = now
                self.coins[c["mint"]] = c
                h = self.hist.setdefault(c["mint"], deque(maxlen=400))
                if not h or h[-1][1] != c["mcap"] or now - h[-1][0] > 15:
                    h.append((now, c["mcap"]))
            self.version += 1

    def _prune(self):
        pc = self.cfg.get("pulse", {})
        limit = pc.get("keep_hours", 6) * 60
        max_coins = pc.get("max_coins", 1500)
        now_ms = time.time() * 1000
        with self.lock:
            drop = [m for m, c in self.coins.items()
                    if (now_ms - c["created_ms"]) / 60000 > limit and m != self.selected]
            for m in drop:
                self.coins.pop(m, None)
                self.hist.pop(m, None)
            if len(self.coins) > max_coins:
                # vyhodit nejdéle neobchodované
                ranked = sorted(self.coins.values(), key=lambda c: c.get("last_trade_ms") or 0)
                for c in ranked[: len(self.coins) - max_coins]:
                    if c["mint"] != self.selected:
                        self.coins.pop(c["mint"], None)
                        self.hist.pop(c["mint"], None)
            if drop:
                self.version += 1

    def _rugcheck_loop(self):
        """Kontroluje RugCheckem jen coiny, které v Soon/Migrated opravdu vidíš (projdou filtry),
        max `rugcheck_per_min` za minutu – zbytek limitu nechává scanneru."""
        done_times: deque = deque()
        while not self.stop_event.is_set():
            pc = self.cfg.get("pulse", {})
            per_min = pc.get("rugcheck_per_min", 10)
            now = time.time()
            while done_times and now - done_times[0] > 60:
                done_times.popleft()
            sel = self.selected
            with self.lock:
                sel_todo = sel if sel in self.coins and "rc" not in self.coins[sel] else None
            # vybraný coin má přednost a nepočítá se do minutového limitu
            if not sel_todo and (per_min <= 0 or len(done_times) >= per_min or rugcheck.cooldown_left() > 0):
                self.stop_event.wait(2)
                continue
            mint = sel_todo
            if not mint:
                auto_cols = pc.get("auto_rugcheck", ["soon", "migrated"])
                cols_cfg = pc.get("columns", {})
                with self.lock:
                    todo = [c for c in self.coins.values()
                            if "rc" not in c and self.column_of(c) in auto_cols
                            and passes(c, cols_cfg.get(self.column_of(c), {}), {})]
                todo.sort(key=lambda c: -c["mcap"])
                mint = todo[0]["mint"] if todo else None
            if not mint:
                self.stop_event.wait(2)
                continue
            try:
                self.check_risk(mint)
            except rugcheck.RateLimited as e:
                self.log("ERROR", f"Pulse: {e}")
                continue
            if mint != sel_todo:
                done_times.append(time.time())

    def check_risk(self, mint: str):
        rc = rugcheck.report(mint)
        score, label = risk.quick(rc, self.cfg)
        with self.lock:
            if mint in self.coins:
                self.coins[mint] = dict(self.coins[mint], rc=rc, risk_score=score, risk_label=label)
                self.version += 1

    def _pattern_loop(self):
        """Bot filtr: stáhne 1s svíčky a hledá fake vzory. Nejdřív coiny bez kontroly (nové),
        pak periodicky znovu (vzor se může objevit později). Max `checks_per_sec` dotazů/s."""
        while not self.stop_event.is_set():
            bc = self.cfg.get("bot_filter", {})
            if not bc.get("enabled", True):
                self.stop_event.wait(2)
                continue
            if candles.backoff_left() > 0:
                self.stop_event.wait(candles.backoff_left())
                continue
            now = time.time()
            recheck = bc.get("recheck_sec", 90)
            min_age = bc.get("min_age_sec", 45)
            cols_cfg = self.cfg.get("pulse", {}).get("columns", {})
            with self.lock:
                # jen coiny, které by jinak byly vidět (projdou filtry sloupce) – šetří requesty
                todo = [c for c in self.coins.values()
                        if (now * 1000 - c["created_ms"]) / 1000 >= min_age
                        and (c.get("injected") or (self.column_of(c) in bc.get("columns", list(COLUMNS))
                                                   and passes(c, cols_cfg.get(self.column_of(c), {}), {})))
                        and (not c.get("pat_ts") or now - c["pat_ts"] > recheck
                             or (c.get("pat", {}).get("verdict") == "unknown" and now - c["pat_ts"] > 20))]
            if self.selected:
                todo.sort(key=lambda c: c["mint"] != self.selected)
            # nezkontrolované první, pak nejdéle nekontrolované; aktivní (vyšší MC) dřív
            todo.sort(key=lambda c: (c.get("pat_ts") or 0, -c["mcap"]))
            if not todo:
                self.stop_event.wait(1)
                continue
            c = todo[0]
            try:
                data = candles.recent_1s(c["mint"], bc.get("window_sec", 600))
                res = patterns.analyze(data, c["mcap"], bc.get("sensitivity", "střední"))
            except Exception as e:  # noqa: BLE001
                res = {"verdict": "unknown", "score": 0, "reasons": [f"chyba: {e}"], "n": 0}
            with self.lock:
                if c["mint"] in self.coins:
                    old = self.coins[c["mint"]].get("pat") or {}
                    # jednou bot = bot (boti občas na chvíli přestanou, ať coin neproblikává)
                    if old.get("verdict") == "bot" and res["verdict"] != "bot" and bc.get("sticky", True):
                        res = {**old, "reasons": old["reasons"]}
                    self.coins[c["mint"]] = dict(self.coins[c["mint"]], pat=res, pat_ts=time.time())
                    self.version += 1
            self.stop_event.wait(1 / max(0.2, bc.get("checks_per_sec", 2)))

    def _gem_loop(self):
        """Počítá gem skóre (zdarma) a nejlepší kandidáty pošle na AI pipeline (screen -> report)."""
        while not self.stop_event.is_set():
            g = self.cfg.get("gems", {})
            if not g.get("enabled", True):
                self.stop_event.wait(3)
                continue
            cols_cfg = self.cfg.get("pulse", {}).get("columns", {})
            with self.lock:
                coins = list(self.coins.values())
                hist = {c["mint"]: list(self.hist.get(c["mint"], ())) for c in coins}
            scored = []
            seen_rows = []
            for c in coins:
                col = self.column_of(c)
                pat = (c.get("pat") or {}).get("verdict")
                row = {"mint": c["mint"], "symbol": c.get("symbol"), "name": c.get("name"),
                       "created_ms": c.get("created_ms"), "mcap": c.get("mcap"), "progress": c.get("progress"),
                       "bot": pat == "bot", "injected": c.get("injected")}
                seen_rows.append(row)
                if pat == "bot":
                    row["reason"] = "bot filtr: " + ", ".join((c.get("pat") or {}).get("reasons", [])[:2])
                    continue
                # coiny ze scanneru (i s nízkým MC) jdou do gem skóre vždy, ostatní podle filtrů sloupců
                if not c.get("injected"):
                    if col not in g.get("columns", list(COLUMNS)):
                        row["reason"] = f"sloupec {col} vypnutý pro gemy"
                        continue
                    if not passes(c, cols_cfg.get(col, {}), {}):
                        row["reason"] = f"filtr sloupce {col}"
                        continue
                if not gems.is_candidate(c, self.cfg):
                    row["reason"] = ("bot filtr ještě nezkontroloval" if pat != "ok"
                                     else "mimo MC/stáří pro gemy")
                    continue
                sc, why = gems.score(c, hist.get(c["mint"], []), self.cfg)
                if c.get("injected"):
                    why = why + ["našel scanner"]
                wt, bt = g.get("watch_score", 50), g.get("buy_score", 65)
                row.update(score=sc, candidate=True, hot=sc >= wt,
                           reason="low skóre (bez AI)" if sc < wt else "watch skóre (Jev)" if sc < bt else "buy skóre (Jev + report)")
                scored.append((sc, why, c))
            if time.time() - self._seen_saved > 20:
                self._seen_saved = time.time()
                try:
                    gems.record_seen(seen_rows)
                except Exception as e:  # noqa: BLE001
                    self.log("ERROR", f"seen DB: {e}")
            with self.lock:
                for sc, why, c in scored:
                    if c["mint"] in self.coins:
                        self.coins[c["mint"]] = dict(self.coins[c["mint"]], gem=sc, gem_why=why)
                if scored:
                    self.version += 1
            # stavový automat SHIT / LONG / SHORT / GEM / ARCHIV – rozhoduje o Jevu a AI
            try:
                with self.lock:
                    all_coins = dict(self.coins)
                self.watch.step(scored, all_coins)
            except Exception as e:  # noqa: BLE001
                self.log("ERROR", f"watch: {e}")
            self.stop_event.wait(2)

    def has_jev(self) -> bool:
        return llm.has_jev() and self.cfg.get("llm", {}).get("enabled", True)

    def _jev_budget_ok(self, priority: str = "high") -> bool:
        """Max Jev za minutu. SHIT/LONG ('low') smí jen část limitu – zbytek je rezerva pro SHORT a GEM."""
        now = time.time()
        while self.jev_times and now - self.jev_times[0] > 60:
            self.jev_times.popleft()
        cap = self.cfg.get("gems", {}).get("max_jev_per_min", 30)
        if priority == "low":
            cap = cap * self.watch.cfg["jev_low_share"] / 100
        return len(self.jev_times) < cap and len(self.jev_inflight) < 6

    def request_report(self, sc: int, why: list, c: dict):
        """SHORT coin dosáhl BUY skóre -> AI report (free, placený jen pojistka)."""
        g = self.cfg.get("gems", {})
        mint = c["mint"]
        if mint in self.ai_inflight or len(self.ai_inflight) >= g.get("ai_parallel", 3):
            return
        prev = self.gem_analyzed.get(mint)
        if prev and (time.time() - prev[0] < g.get("reanalyze_min", 30) * 60 or sc < prev[1] + 15):
            return
        self.gem_analyzed[mint] = (time.time(), sc)
        self.ai_inflight.add(mint)
        self.ai_pool.submit(self._analyze_gem_safe, sc, why, c, self.gem_ids.get(mint))

    def _jev_task(self, sc, why, c):
        mint = c["mint"]
        self.jev_times.append(time.time())
        j = None
        try:
            j = llm.jev_screen(*self._ai_input(c, sc, why), self.cfg)
            upd = {"jev": j, "jev_ts": time.time(), "jev_error": None}
        except Exception as e:  # noqa: BLE001
            upd = {"jev_ts": time.time(), "jev_error": str(e)[:120]}
            self.gem_status = f"Jev chyba: {str(e)[:70]}"
        finally:
            self.jev_inflight.discard(mint)
        with self.lock:
            if mint in self.coins:
                self.coins[mint] = dict(self.coins[mint], **upd)
                self.version += 1
        try:
            self.watch.on_jev(mint, j)
        except Exception as e:  # noqa: BLE001
            self.log("ERROR", f"watch jev: {e}")

    def _analyze_gem_safe(self, sc, why, c, gid):
        try:
            self._analyze_gem(sc, why, c, gid)
        except Exception as e:  # noqa: BLE001
            self.log("ERROR", f"Gem {c.get('symbol')}: {e}")
        finally:
            self.ai_inflight.discard(c["mint"])

    def retry_gem(self, gid: int, mint: str):
        """Gem bez AI (limit/chyba) – tracker ho po 5 min pošle znovu."""
        if mint in self.ai_inflight:
            return
        with self.lock:
            c = self.coins.get(mint)
        if not c:
            try:
                c = pumpfun.coin(mint)
            except Exception:  # noqa: BLE001
                c = None
            if not c:
                return
        self.ai_inflight.add(mint)
        self.ai_pool.submit(self._analyze_gem_safe, c.get("gem") or 0, c.get("gem_why") or [], c, gid)

    def inject_async(self, mint: str, source: str = "scanner"):
        self._pool.submit(self.inject, mint, source)

    def inject(self, mint: str, source: str = "scanner"):
        """Coin ze scanneru -> do Pulse paměti, ať ho vidí gem detektor (i když ho filtry sloupců skrývají)."""
        with self.lock:
            if mint in self.coins:
                self.coins[mint] = dict(self.coins[mint], injected=source)
                return
        try:
            c = pumpfun.coin(mint)
        except Exception:  # noqa: BLE001
            return
        if not c:
            return
        c["injected"] = source
        self._merge([c])

    def _analyze_gem(self, sc: int, why: list[str], c: dict, gid: int | None = None):
        """Report pro buy skóre: free model (hlavní) -> placený jen když free nejde."""
        g = self.cfg.get("gems", {})
        mint = c["mint"]
        if "rc" not in c:
            try:
                self.check_risk(mint)
            except rugcheck.RateLimited:
                pass
            with self.lock:
                c = self.coins.get(mint, c)
        if c.get("risk_label") == "HIGH" or (c.get("rc") or {}).get("mint_authority"):
            self.gem_status = f"{c['symbol']}: vysoké riziko, AI přeskočena"
            stored = {"verdict": "SKIP", "confidence": 1.0, "safety": "DANGER",
                      "summary": "vysoké riziko (RugCheck) – AI přeskočena"}
            if gid is None:
                self.gem_ids[mint] = gems.save(c, sc, why, stored, "pulse", False, stage="risk", jev=c.get("jev"))
            else:
                gems.update_ai(gid, stored, "risk", None, False)
            return
        ai = None
        t0 = time.time()
        if not (g.get("auto_ai", True) and self.cfg.get("llm", {}).get("enabled", True)):
            self.gem_status = f"{c['symbol']} gem {sc} – auto AI vypnutá"
        else:
            self.gem_status = f"🤖 report {c['symbol']} (gem {sc}, Jev {(c.get('jev') or {}).get('score', '–')})…"
            try:
                ai = llm.report(*self._ai_input(c, sc, why), self.cfg)
                self.gem_status = (f"{c['symbol']}: {ai['verdict']} {ai['confidence'] * 100:.0f} % "
                                   f"({time.time() - t0:.0f} s, {ai['model'].split('/')[-1]}"
                                   + (", placený – free nešel" if ai.get("paid") else "") + ")")
            except Exception as e:  # noqa: BLE001
                self.gem_status = f"AI nejde: {str(e)[:90]}"
                self.log("ERROR", f"Gem AI {c['symbol']}: {e}")
        with self.lock:
            if mint in self.coins:
                self.coins[mint] = dict(self.coins[mint], ai=ai)
                self.version += 1
        pop = False
        if ai:
            pop = ai["verdict"] in g.get("popup_verdicts", ["BUY"]) and ai["confidence"] >= g.get("min_confidence", 0.55) \
                and ai.get("safety") != "DANGER"
        elif sc >= g.get("popup_score_without_ai", 80):
            pop = True
        stage = "report" if ai else "no_ai"
        if gid is None:
            gid = gems.save(c, sc, why, ai, "scanner" if c.get("injected") else "pulse", pop, stage=stage, jev=c.get("jev"))
            self.gem_ids[mint] = gid
        else:
            gems.update_ai(gid, ai, stage, None, pop)
        try:
            self.watch.on_ai(mint, ai, gid)
        except Exception as e:  # noqa: BLE001
            self.log("ERROR", f"watch ai: {e}")
        cost = (ai or {}).get("cost", 0)
        self.log("HIT" if pop else "INFO", f"💎 {c['symbol']} gem {sc} · Jev {(c.get('jev') or {}).get('score', '–')}"
                 + (f" | {ai['verdict']} {ai['confidence'] * 100:.0f} % – {ai['summary']}" if ai else " | AI nešla")
                 + (f" (${cost:.4f}, placený)" if (ai or {}).get("paid") else ""))
        if pop:
            self.on_gem({"id": gid, "coin": c, "score": sc, "why": why, "ai": ai, "jev": c.get("jev"),
                         "plan": riskmgmt.plan(self.cfg, c.get("risk_label"), c.get("mcap"))})

    def _ai_input(self, c: dict, sc: int, why: list[str]):
        d = c.get("dex") or {}
        pat = c.get("pat") or {}
        plan = riskmgmt.plan(self.cfg, c.get("risk_label"), c.get("mcap"))
        metrics = {
            "symbol": c["symbol"], "name": c["name"], "age_min": round(c["age_min"], 1),
            "mcap_usd": round(c["mcap"]), "ath_mcap_usd": round(c["ath_mcap"]),
            "bonding_progress_pct": round(c["progress"], 1), "migrated": c["complete"],
            "sol_in_curve": c.get("sol_in_curve"), "replies": c["replies"], "is_live": c.get("is_live"),
            "liquidity": d.get("liquidity"), "volume_h1": d.get("volume_h1"),
            "buys_m5": d.get("buys_m5"), "sells_m5": d.get("sells_m5"), "price_change": d.get("price_change"),
            "activity_10min": pat.get("m"), "bot_filter": {"verdict": pat.get("verdict"), "score": pat.get("score"),
                                                           "notes": pat.get("reasons")},
            "gem_score": sc, "gem_reasons": why,
            "user_position_plan": {"size_sol": plan["size_sol"], "stop_loss_pct": plan["stop_loss_pct"],
                                   "take_profits": [f"{t['multiple']}× prodat {t['sell_pct']:.0f} %"
                                                    for t in plan["take_profits"]]},
            "websites": [c["website"]] if c.get("website") else [],
            "socials": [{"type": t, "url": c[t]} for t in ("twitter", "telegram") if c.get(t)],
        }
        profile = {"description": c.get("description", ""), "links": []}
        return profile, metrics, c.get("rc")

    # ---------- BUY gemy: hluboké sledování po cyklech ----------
    def _buy_loop(self):
        """Každý BUY gem v hlubokém režimu: 1 cyklus = buy_tracking.cycle_sec (30 s).
        Každý cyklus cena (bez AI), Jev každých N cyklů (max M), AI update každých K cyklů (max L).
        Po deep_cycles cyklech přejde na běžné sledování (1 min)."""
        while not self.stop_event.is_set():
            b = self.cfg.get("buy_tracking", {})
            if not b.get("enabled", True):
                self.stop_event.wait(5)
                continue
            try:
                rows = [dict(r) for r in gems.db.query(
                    "SELECT * FROM gems WHERE outcome='pending' AND verdict='BUY' AND COALESCE(archived,0)=0 "
                    "AND COALESCE(cycles,0) < ?", (b.get("deep_cycles", 120),))]
            except Exception:  # noqa: BLE001
                rows = []
            now = time.time()
            for r in rows:
                if r["id"] in self.buy_inflight:
                    continue
                if r["last_checked"] and now - r["last_checked"] < b.get("cycle_sec", 30) - 1:
                    continue
                self.buy_inflight.add(r["id"])
                self.buy_pool.submit(self._buy_cycle, r)
            self.stop_event.wait(2)

    def _buy_cycle(self, r: dict):
        b = self.cfg.get("buy_tracking", {})
        dur = self.cfg.get("tracking", {}).get("duration_hours", 24) * 3600
        try:
            c = pumpfun.coin(r["address"])
            if not c or not r["mcap0"]:
                gems.update_row(r["id"], {"last_checked": time.time()})
                return
            cyc = (r["cycles"] or 0) + 1
            f = gems.price_fields(r, c, time.time(), dur)
            f["cycles"] = cyc
            if cyc >= b.get("deep_cycles", 120) and "outcome" not in f:
                f["stop_reason"] = f"hluboké sledování skončilo po {cyc} cyklech – dál každou minutu"
            gems.update_row(r["id"], f)
            gems.add_update(r["id"], cyc, "price", c["mcap"], f["last_mult"])
            if "outcome" in f:
                return
            with self.lock:
                pc = self.coins.get(r["address"])
            coin = {**c, **({k: pc[k] for k in ("rc", "risk_label", "pat", "dex", "gem", "gem_why") if k in pc} if pc else {})}
            # Jev každých N cyklů
            if self.has_jev() and b.get("jev_every_cycles", 4) > 0 \
                    and cyc % max(1, b.get("jev_every_cycles", 4)) == 0 and (r["jev_updates"] or 0) < b.get("jev_max_updates", 60):
                try:
                    j = llm.jev_screen(*self._ai_input(coin, r["gem_score"] or 0, []), self.cfg)
                    gems.update_row(r["id"], {"jev_score": j["score"], "jev_action": j["hint"], "jev_reason": j["reason"],
                                              "jev_updates": (r["jev_updates"] or 0) + 1})
                    gems.add_update(r["id"], cyc, "jev", c["mcap"], f["last_mult"], j["score"], j["hint"],
                                    {"reason": j["reason"]})
                    with self.lock:
                        if r["address"] in self.coins:
                            self.coins[r["address"]] = dict(self.coins[r["address"]], jev=j, jev_ts=time.time())
                            self.version += 1
                    r["jev_score"] = j["score"]
                except Exception:  # noqa: BLE001
                    pass
            # AI update každých K cyklů
            if b.get("ai_updates", True) and cyc % max(1, b.get("ai_every_cycles", 10)) == 0 \
                    and (r["ai_updates"] or 0) < b.get("ai_max_updates", 6):
                self._buy_ai_update(r, coin, cyc, f)
        except Exception as e:  # noqa: BLE001
            self.log("ERROR", f"BUY tracking {r.get('symbol')}: {e}")
        finally:
            self.buy_inflight.discard(r["id"])

    def _buy_ai_update(self, r: dict, coin: dict, cyc: int, f: dict):
        b = self.cfg.get("buy_tracking", {})
        hist = gems.get_updates(r["id"], ("price", "jev", "ai"))
        prices = [(h["cycle"], round(h["mcap"] or 0)) for h in hist if h["kind"] == "price"]
        step = max(1, len(prices) // 20)
        payload = {
            "coin": {"symbol": r["symbol"], "name": r["name"]},
            "alert": {"mcap": r["mcap0"], "gem_score": r["gem_score"], "jev_score_at_alert": r.get("jev0"),
                      "summary": r["summary"], "thesis": r["thesis"], "strategy": json.loads(r["strategy"] or "{}")},
            "now": {"cycle": cyc, "cycle_sec": b.get("cycle_sec", 30), "mcap": coin["mcap"],
                    "mult_from_alert": round(f["last_mult"], 2), "peak_mult": round(f["peak_mult"], 2),
                    "ath_mcap": coin.get("ath_mcap"), "bonding_progress": coin.get("progress"),
                    "migrated": coin.get("complete"), "replies": coin.get("replies"),
                    "jev_score": r.get("jev_score"), "bot_filter": (coin.get("pat") or {}).get("reasons")},
            "mcap_by_cycle": prices[::step][-20:],
            "jev_by_cycle": [(h["cycle"], h["jev_score"]) for h in hist if h["kind"] == "jev"][-15:],
            "previous_ai_updates": [json.loads(h["data"] or "{}").get("summary") for h in hist if h["kind"] == "ai"][-3:],
        }
        try:
            u = llm.ai_update(payload, self.cfg, allow_paid=b.get("ai_allow_paid", False))
        except Exception as e:  # noqa: BLE001
            self.gem_status = f"AI update {r['symbol']}: {str(e)[:70]}"
            return
        text = f"{u['action']} · trend {u['trend']} · {u['summary']}"
        gems.update_row(r["id"], {"ai_updates": (r["ai_updates"] or 0) + 1, "last_action": u["action"],
                                  "last_update_text": text, "cost": (r["cost"] or 0) + (u.get("cost") or 0)})
        gems.add_update(r["id"], cyc, "ai", coin["mcap"], f["last_mult"], r.get("jev_score"), u["action"], u)
        self.gem_status = f"🔄 {r['symbol']}: {u['action']} ({u['trend']}) – {u['summary'][:60]}"
        self.log("HIT" if u["action"] in ("SELL", "TAKE_PROFIT") else "INFO",
                 f"🔄 {r['symbol']} cyklus {cyc}: {text}" + (" [placený]" if u.get("paid") else ""))
        if u["action"] == "SELL":
            self.watch.on_gem_exit(r["address"], "AI update: SELL")
        if u["action"] in b.get("popup_actions", ["SELL", "TAKE_PROFIT"]) and u["action"] != r.get("last_action"):
            self.on_gem_update({"id": r["id"], "coin": {**coin, "mint": r["address"], "symbol": r["symbol"],
                                                        "name": r["name"]},
                                "update": u, "cycle": cyc, "mult": f["last_mult"], "ai": None, "score": r["gem_score"]})

    def _detail_loop(self):
        """Vybraný coin: cena každou 1 s (hýbe poslední svíčkou), DexScreener data po migraci."""
        tick = 0
        while not self.stop_event.is_set():
            mint = self.selected
            if mint:
                try:
                    c = pumpfun.coin(mint)
                    if c:
                        self._merge([c])
                        self.chart.push_price(mint, c["mcap"])
                    if c and c["complete"] and tick % 5 == 0:
                        pair = dex.pairs_for_tokens("solana", [mint]).get(mint)
                        if pair:
                            with self.lock:
                                if mint in self.coins:
                                    self.coins[mint] = dict(self.coins[mint], dex=dex.pair_metrics(pair))
                                    self.version += 1
                except Exception:  # noqa: BLE001
                    pass
            tick += 1
            self.stop_event.wait(1)


# ---------- filtrování ----------
def passes(c: dict, col_cfg: dict, quick: dict) -> bool:
    """col_cfg = filtry sloupce z configu, quick = rychlé filtry z lišty v GUI."""
    q = (quick.get("search") or "").lower().strip()
    if q and q not in c["symbol"].lower() and q not in c["name"].lower() and q not in c["mint"].lower():
        return False
    if quick.get("socials_only") and not pumpfun.has_socials(c):
        return False
    if quick.get("hide_high") and c.get("risk_label") == "HIGH":
        return False
    pat = (c.get("pat") or {}).get("verdict")
    if quick.get("hide_bots") and pat == "bot":
        return False
    if quick.get("hide_unchecked") and not c.get("pat_ts"):
        return False
    if c["mcap"] < max(col_cfg.get("min_mcap", 0), quick.get("min_mcap", 0)):
        return False
    if col_cfg.get("max_mcap") and c["mcap"] > col_cfg["max_mcap"]:
        return False
    if col_cfg.get("max_age_min") and c["age_min"] > col_cfg["max_age_min"]:
        return False
    if col_cfg.get("min_progress") and c["progress"] < col_cfg["min_progress"]:
        return False
    if col_cfg.get("require_socials") and not pumpfun.has_socials(c):
        return False
    if col_cfg.get("min_replies") and c["replies"] < col_cfg["min_replies"]:
        return False
    order = ["LOW", "MEDIUM", "HIGH"]
    mr = col_cfg.get("max_risk")
    if mr in order and c.get("risk_label") in order and order.index(c["risk_label"]) > order.index(mr):
        return False
    words = [w.strip().lower() for w in (col_cfg.get("hide_keywords") or "").split(",") if w.strip()]
    text = f"{c['name']} {c['symbol']}".lower()
    if any(w in text for w in words):
        return False
    return True


def sort_key(col: str, c: dict):
    if col == "new":
        return -c["created_ms"]
    if col == "soon":
        return -c["progress"]
    return -(c.get("migrated_at") or 0)
