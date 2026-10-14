"""Hlavní smyčka: scan nových coinů -> filtry -> RugCheck -> LLM -> risk -> DB -> alert,
a paralelně tracker, který sleduje cenu uložených coinů a vyhodnocuje predikce."""
import threading
import time
import traceback
from typing import Callable

from . import alerts, candles, db, dexscreener as dex, filters, llm, patterns, pumpfun, risk, rugcheck
from .config import load_config

Log = Callable[[str, str], None]  # (level, message)


class Engine:
    def __init__(self, log: Log, on_change: Callable[[], None] = lambda: None):
        self.log = log
        self.on_change = on_change
        self.on_gem = lambda gem: None
        self.on_candidate = lambda mint: None   # Pulse si coin převezme do gem detektoru
        self.stop_event = threading.Event()
        self.cfg: dict = {}
        self.retries: dict[str, int] = {}     # coiny odložené kvůli rate limitu
        self.rc_cache: dict[str, dict] = {}   # RugCheck výsledky odložených coinů (ať se neptáme 2×)
        self.llm_cooldown_until = 0.0
        self.llm_calls = 0
        self._ignored = 0

    # ---------- řízení ----------
    def run(self):
        db.init()
        self.cfg = load_config()
        self.log("INFO", "Scanner spuštěn.")
        sc = self.cfg.get("scanner", {})
        tr = self.cfg.get("tracking", {})
        next_scan = next_track = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            try:
                if now >= next_scan:
                    self.scan_once()
                    next_scan = time.time() + sc.get("scan_interval_sec", 60)
                if now >= next_track:
                    self.track_once()
                    next_track = time.time() + tr.get("interval_sec", 300)
            except Exception as e:  # noqa: BLE001
                self.log("ERROR", f"{e}\n{traceback.format_exc(limit=2)}")
                next_scan = time.time() + 30
            self.stop_event.wait(1)
        self.log("INFO", "Scanner zastaven.")

    def stop(self):
        self.stop_event.set()

    # ---------- scan ----------
    def scan_once(self):
        sc = self.cfg.get("scanner", {})
        chain = sc.get("chain", "solana")
        sources = sc.get("sources", ["token_profiles"])
        cands = dex.latest_candidates(chain, sources)
        if "pumpfun" in sources:
            try:
                min_prog = sc.get("pumpfun_min_progress", 70)
                for c in pumpfun.list_coins("last_trade_timestamp", 50):
                    if (c["complete"] or c["progress"] >= min_prog) and c["mint"] not in cands:
                        cands[c["mint"]] = {
                            "tokenAddress": c["mint"], "description": c["description"], "_source": "pumpfun",
                            "links": [{"url": u} for u in (c["twitter"], c["telegram"], c["website"]) if u],
                        }
            except Exception as e:  # noqa: BLE001
                self.log("ERROR", f"pump.fun zdroj: {e}")
        known = db.known_addresses(list(cands))
        new = [a for a in cands if a not in known]
        self.log("INFO", f"Scan: {len(cands)} kandidátů, {len(new)} nových.")
        if not new:
            return
        pairs = dex.pairs_for_tokens(chain, new)
        self._ignored = 0
        for addr in new:
            if self.stop_event.is_set():
                return
            try:
                self.process(addr, cands[addr], pairs.get(addr))
            except Exception as e:  # noqa: BLE001
                self.log("ERROR", f"{addr[:8]}…: {e}")
        if self._ignored:
            self.log("INFO", f"Ignorováno {self._ignored} starých coinů s malým MC (stale filtr).")
        self.on_change()

    def process(self, addr: str, profile: dict, pair: dict | None):
        base = {"address": addr, "source": profile.get("_source"), "first_seen": time.time()}
        if pair is None:
            # ještě nemá pár – zkusíme ho příště (neukládáme)
            return
        m = dex.pair_metrics(pair)
        track_until = time.time() + self.cfg.get("tracking", {}).get("duration_hours", 24) * 3600
        row = {
            **base,
            "symbol": m["symbol"], "name": m["name"], "pair_address": m["pair_address"],
            "dex_id": m["dex_id"], "url": m["url"], "pair_created_at": m["pair_created_at"],
            "price0": m["price"], "mcap0": m["mcap"], "liq0": m["liquidity"],
            "vol_h1_0": m["volume_h1"], "age_min0": m["age_min"],
            "last_price": m["price"], "peak_price": m["price"], "peak_mult": 1.0, "last_mult": 1.0,
            "min_mult": 1.0, "track_until": track_until,
        }
        track_rejected = self.cfg.get("tracking", {}).get("track_rejected", True)
        tag = f"{m['symbol'] or '?'} ({addr[:6]}…)"

        # 0) staré coiny bez růstu vůbec nebereme (neukládají se, jen zaplevelují DB)
        skip = filters.ignore(m, self.cfg)
        if skip:
            self._ignored += 1
            return
        # každý rozumný coin (i s nízkým MC, i když ho filtry vyhodí) dostane šanci v gem detektoru
        gc = self.cfg.get("gems", {})
        if gc.get("from_scanner", True) and (m["mcap"] or 0) <= (gc.get("max_mcap") or 1e18):
            try:
                self.on_candidate(addr)
            except Exception:  # noqa: BLE001
                pass

        # 1) základní filtry
        reason = filters.basic(m, self.cfg)
        if reason:
            self._save(row, "rejected_basic", reason, track_rejected)
            self.log("SKIP", f"{tag} – {reason}")
            return

        # 1b) bot filtr – zdarma, před RugCheckem a AI (šetří limity)
        bc = self.cfg.get("bot_filter", {})
        if bc.get("enabled", True) and bc.get("scanner", True):
            try:
                pat = patterns.analyze(candles.recent_1s(addr, bc.get("window_sec", 600)), m["mcap"],
                                       bc.get("sensitivity", "střední"))
            except Exception:  # noqa: BLE001
                pat = None      # graf nedostupný (např. ne-pump.fun token) -> filtr přeskočit
            if pat and pat["verdict"] == "bot":
                reason = "BOT: " + "; ".join(pat["reasons"])
                self._save(row, "rejected_pattern", reason, track_rejected)
                self.log("SKIP", f"{tag} – {reason}")
                return

        # 2) bezpečnost
        rc = None
        lc = self.cfg.get("llm", {})
        if lc.get("enabled", True) and time.time() < self.llm_cooldown_until:
            # AI má pauzu -> nemá smysl pálit RugCheck, coin zkusíme po pauze
            self._postpone(addr, tag, f"AI rate limit, pauza ještě {self.llm_cooldown_until - time.time():.0f}s")
            return
        if addr in self.rc_cache:
            rc = self.rc_cache[addr]
        elif self.cfg.get("safety_filters", {}).get("enabled", True):
            try:
                rc = rugcheck.report(addr)
            except rugcheck.RateLimited as e:
                self._postpone(addr, tag, f"RugCheck: {e}")
                return
            if rc is None and self._postpone(addr, tag, f"RugCheck {rugcheck.last_status}", max_tries=2):
                return
        if rc:
            self.rc_cache[addr] = rc
            row.update(rug_score=rc["score"], mint_auth=int(rc["mint_authority"]),
                       freeze_auth=int(rc["freeze_authority"]), top10_pct=rc["top10_pct"],
                       rug_risks=rc["risks"])
        reason = filters.safety(rc, self.cfg)
        if reason:
            row["risk_score"], row["risk_label"] = risk.rate(m, rc, None, self.cfg)
            self._save(row, "rejected_safety", reason, track_rejected)
            self.log("SKIP", f"{tag} – {reason}")
            return

        # 3) LLM
        ai = None
        if lc.get("enabled", True):
            if time.time() < self.llm_cooldown_until:
                self._postpone(addr, tag, f"AI rate limit, pauza ještě {self.llm_cooldown_until - time.time():.0f}s")
                return
            try:
                self.llm_calls += 1
                ai, scr = llm.pipeline(profile, m, rc, self.cfg)
                if ai is None:
                    reason = f"Jev: {scr.get('reason', '')}"
                    row["risk_score"], row["risk_label"] = risk.rate(m, rc, None, self.cfg)
                    self._save(row, "rejected_llm", reason, track_rejected)
                    self.log("SKIP", f"{tag} – {reason}")
                    return
                row.update(llm_verdict=ai["verdict"], llm_confidence=ai["confidence"],
                           llm_summary=ai["summary"], llm_flags=ai["red_flags"],
                           llm_narrative=ai["narrative"])
            except Exception as e:  # noqa: BLE001
                if "429" in str(e):
                    # free limit OpenRouteru – pauza 10 min, coin zkusíme znovu
                    self.llm_cooldown_until = time.time() + 600
                    self.log("ERROR", f"OpenRouter rate limit (AI dotazů v této relaci: {self.llm_calls}). "
                                      "AI pauza 10 min.")
                    self._postpone(addr, tag, "AI rate limit")
                    return
                # jiná chyba – neukládat jako "passed" bez AI kontroly
                row["risk_score"], row["risk_label"] = risk.rate(m, rc, None, self.cfg)
                self._save(row, "llm_error", f"LLM selhalo: {e}", track_rejected)
                self.log("ERROR", f"{tag} – LLM selhalo: {e}")
                return

        row["risk_score"], row["risk_label"] = risk.rate(m, rc, ai, self.cfg)

        if ai and (ai["verdict"] == "SKIP" or ai["confidence"] < lc.get("min_confidence", 0)):
            reason = f"AI {ai['verdict']} ({ai['confidence']:.2f}): {ai['summary']}"
            self._save(row, "rejected_llm", reason, track_rejected)
            self.log("SKIP", f"{tag} – {reason}")
            return

        coin_id = self._save(row, "passed", None, True)
        self._maybe_gem(addr, profile, m, rc, ai, row)
        verdict = ai["verdict"] if ai else "N/A"
        self.log("HIT", f"✅ {tag} | {verdict} | risk {row['risk_label']} {row['risk_score']} | "
                        f"mcap ${m['mcap'] or 0:,.0f} | {m['url']}")
        self._maybe_alert(coin_id, row)

    def _maybe_gem(self, addr, profile, m, rc, ai, row):
        """Scanner našel coin, který AI doporučila koupit -> uložit jako gem + popup."""
        g = self.cfg.get("gems", {})
        if not ai or ai["verdict"] not in g.get("popup_verdicts", ["BUY"]):
            return
        if ai["confidence"] < g.get("min_confidence", 0.55) or ai.get("safety") == "DANGER":
            return
        links = {"twitter": "", "telegram": "", "website": ""}
        for s_ in m.get("socials") or []:
            if s_.get("type") in links:
                links[s_["type"]] = s_.get("url") or ""
        if m.get("websites"):
            links["website"] = m["websites"][0]
        coin = {"mint": addr, "symbol": m["symbol"] or "?", "name": m["name"] or "", "mcap": m["mcap"] or 0,
                "progress": 100.0 if m["dex_id"] != "pumpfun" else 0.0, "complete": m["dex_id"] != "pumpfun",
                "risk_label": row.get("risk_label"), "age_min": m["age_min"] or 0, "ath_mcap": m["mcap"] or 0,
                "replies": 0, "description": profile.get("description", ""), **links,
                "dex": m}
        from . import gems, riskmgmt
        why = ["scanner: prošel všemi filtry"]
        gid = gems.save(coin, 0, why, ai, "scanner", True)
        self.on_gem({"id": gid, "coin": coin, "score": None, "why": why, "ai": ai,
                     "plan": riskmgmt.plan(self.cfg, row.get("risk_label"), m["mcap"])})

    def _postpone(self, addr: str, tag: str, why: str, max_tries: int = 10) -> bool:
        """Coin neukládáme, zkusí se při dalším scanu. Vrací False, když už je pokusů moc."""
        n = self.retries.get(addr, 0) + 1
        if n > max_tries:
            self.retries.pop(addr, None)
            return False
        self.retries[addr] = n
        self.log("INFO", f"{tag} – odloženo ({why}), pokus {n}/{max_tries}")
        return True

    def _save(self, row: dict, stage: str, reason: str | None, track: bool) -> int:
        self.retries.pop(row["address"], None)
        self.rc_cache.pop(row["address"], None)
        row = {**row, "stage": stage, "reject_reason": reason}
        if not track:
            row["outcome"] = "untracked"
        coin_id = db.insert_coin(row)
        if coin_id and track and row.get("price0"):
            db.add_snapshot(coin_id, row["price0"], row["mcap0"], row["liq0"])
        return coin_id

    def _maybe_alert(self, coin_id: int, row: dict):
        ac = self.cfg.get("alerts", {})
        if not ac.get("discord_enabled"):
            return
        order = ["LOW", "MEDIUM", "HIGH"]
        max_label = ac.get("max_risk_label", "MEDIUM")
        if row.get("llm_verdict") not in ac.get("only_verdict", ["BUY"]):
            return
        if order.index(row["risk_label"]) > order.index(max_label):
            return
        try:
            if alerts.send(row):
                db.update_coin(coin_id, alerted=1)
                self.log("INFO", f"Discord alert odeslán: {row['symbol']}")
        except Exception as e:  # noqa: BLE001
            self.log("ERROR", f"Discord alert selhal: {e}")

    # ---------- tracking ----------
    def track_once(self):
        tr = self.cfg.get("tracking", {})
        target = tr.get("target_multiple", 2.0)
        rug_drop = tr.get("rug_drop_pct", 90) / 100
        now = time.time()
        rows = db.active_tracked(now)
        if not rows:
            return
        chain = self.cfg.get("scanner", {}).get("chain", "solana")
        pairs = dex.pairs_for_tokens(chain, [r["address"] for r in rows])
        finished = hits = 0
        updates, snaps = [], []
        for r in rows:
            pair = pairs.get(r["address"])
            fields: dict = {"last_checked": now}
            if pair is None:
                # pár zmizel -> bereme jako rug, pokud už je po nějaké době
                if now - r["first_seen"] > 1800:
                    fields["outcome"] = "rug" if (r["peak_mult"] or 1) < target else "hit"
                    finished += 1
                updates.append((r["id"], fields))
                continue
            m = dex.pair_metrics(pair)
            price = m["price"]
            if price and r["price0"]:
                mult = price / r["price0"]
                peak = max(r["peak_mult"] or 1, mult)
                fields.update(last_price=price, last_mcap=m["mcap"], last_liq=m["liquidity"],
                              last_mult=mult, peak_mult=peak, min_mult=min(r["min_mult"] or 1, mult),
                              peak_price=max(r["peak_price"] or 0, price))
                snaps.append((r["id"], now, price, m["mcap"], m["liquidity"]))
                liq_gone = (m["liquidity"] or 0) < (r["liq0"] or 0) * (1 - rug_drop)
                if peak >= target:
                    fields["outcome"] = "hit"
                    hits += 1
                elif mult <= 1 - rug_drop or liq_gone:
                    fields["outcome"] = "rug"
                elif now >= (r["track_until"] or 0):
                    fields["outcome"] = "miss"
                if "outcome" in fields:
                    finished += 1
            updates.append((r["id"], fields))
        db.apply_tracking(updates, snaps)
        self.log("INFO", f"Tracking: aktualizováno {len(rows)} coinů, uzavřeno {finished} ({hits} hit).")
        self.on_change()
