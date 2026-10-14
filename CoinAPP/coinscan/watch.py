"""👁 Watch stavy – stavový automat coinů.

Výchozí (gem) skóre se počítá ZDARMA každé ~2 s pro všechny coiny. Stav rozhoduje, kdy se utratí Jev / AI:

  🔴 SHIT   (skóre < WATCH)   -> 1× Jev: dobrý -> SHORT, špatný -> LONG
  🟠 LONG   (top N, max X min) -> jen obyčejné skóre; zlepší se o +P bodů -> Jev: dobrý -> SHORT, špatný -> ARCHIV
                                 zhorší se o −D bodů (2× po sobě) / MC −M % z vrcholu / bot / čas vypršel -> ARCHIV
  🟡 SHORT  (cykly po 30 s)    -> skóre každý cyklus, Jev každý N-tý cyklus
                                 skóre ≥ BUY a Jev ok -> AI report: BUY -> GEM, WATCH -> zůstává, SKIP -> LONG
                                 zhorší se (−D bodů 2× / Jev −J) nebo dlouho nic -> LONG
  🟢 GEM    (BUY)              -> hluboké sledování (buy_tracking): skóre, Jev a AI podle cyklů
                                 AI řekne SELL nebo MC −X % z vrcholu -> SHORT
  📁 ARCHIV                    -> nesleduje se; „Pull archiv“ později zjistí, co coin udělal po vyhození
"""
import json
import time

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mint TEXT, symbol TEXT,
    from_state TEXT, to_state TEXT, reason TEXT, score INTEGER, jev INTEGER, mcap REAL
);
CREATE INDEX IF NOT EXISTS idx_wl_mint ON watch_log(mint);
CREATE TABLE IF NOT EXISTS watch_archive (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, archived_ts REAL, from_state TEXT, reason TEXT,
    mcap_at REAL, score_at INTEGER, jev_at INTEGER, entered_ts REAL, created_ms REAL,
    pulled_ts REAL, mcap_now REAL, peak_after REAL, mult_after REAL, mult_now REAL
);
CREATE INDEX IF NOT EXISTS idx_wa_ts ON watch_archive(archived_ts);
"""

STATE_CZ = {"shit": "🔴 SHIT", "long": "🟠 LONG", "short": "🟡 SHORT", "gem": "🟢 GEM", "archived": "📁 archiv"}
STATE_COL = {"shit": "#ef4444", "long": "#f97316", "short": "#eab308", "gem": "#22c55e", "archived": "#6b7280"}


def init():
    with db.connect() as con:
        con.executescript(SCHEMA)


class WatchManager:
    def __init__(self, store):
        self.s = store
        self.st: dict[str, dict] = {}

    # ------------------------------------------------------------------ pomocné
    @property
    def cfg(self):
        return {**DEFAULTS, **(self.s.cfg.get("watch") or {})}

    def counts(self) -> dict:
        out = {k: 0 for k in STATE_CZ}
        for w in self.st.values():
            out[w["state"]] = out.get(w["state"], 0) + 1
        return out

    def _set(self, mint: str, w: dict, to: str, reason: str, c: dict | None = None):
        fr = w.get("state")
        w.update(state=to, since=time.time(), fail=0, reason=reason)
        if to in ("long", "short"):
            w["entry"] = w.get("last", w.get("entry", 0))      # nová základna pro zlepšení/zhoršení
            w["cycles"] = 0
        mc = (c or {}).get("mcap")
        try:
            with db.connect() as con:
                con.execute("INSERT INTO watch_log (ts, mint, symbol, from_state, to_state, reason, score, jev, mcap) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (time.time(), mint, w.get("symbol"), fr, to, reason, w.get("last"), w.get("jev_last"), mc))
                if to == "archived":
                    con.execute("INSERT OR REPLACE INTO watch_archive (mint, symbol, name, archived_ts, from_state, reason, "
                                "mcap_at, score_at, jev_at, entered_ts, created_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (mint, w.get("symbol"), w.get("name"), time.time(), fr, reason, mc, w.get("last"),
                                 w.get("jev_last"), w.get("first_ts"), w.get("created_ms")))
        except Exception as e:  # noqa: BLE001
            self.s.log("ERROR", f"watch DB: {e}")
        if to in ("short", "gem") or fr == "gem":
            self.s.log("INFO", f"👁 {w.get('symbol')}: {STATE_CZ.get(fr, fr)} → {STATE_CZ[to]} ({reason})")
        with self.s.lock:
            if mint in self.s.coins:
                self.s.coins[mint] = dict(self.s.coins[mint], wstate=to, wreason=reason)
                self.s.version += 1

    def _jev(self, mint: str, c: dict, sc: int, why: list, priority: str) -> bool:
        """Požádá o Jev. Vrací False, když Jev není (klíč / rozpočet) – volající pak jde dál bez něj."""
        w = self.st[mint]
        if w.get("jev_pending"):
            return True
        if not self.s.has_jev() or not self.s._jev_budget_ok(priority):
            return False
        w["jev_pending"] = True
        w["jev_req_last"] = w.get("last")
        self.s.jev_inflight.add(mint)
        self.s.ai_pool.submit(self.s._jev_task, sc, why, c)
        return True

    # ------------------------------------------------------------------ callbacky
    def on_jev(self, mint: str, j: dict | None):
        w = self.st.get(mint)
        if not w:
            return
        w["jev_pending"] = False
        if not j:
            w["jev_failed"] = w.get("jev_failed", 0) + 1
            return
        first = w.get("jev_first") is None
        w["jev_n"] = w.get("jev_n", 0) + 1
        w["jev_last"] = j["score"]
        w["jev_best"] = max(w.get("jev_best") or 0, j["score"])
        if first:
            w["jev_first"] = j["score"]
        cfg = self.cfg
        with self.s.lock:
            c = self.s.coins.get(mint)
        if w["state"] == "shit":
            if j["score"] >= cfg["jev_good"]:
                self._set(mint, w, "short", f"Jev {j['score']} na startu", c)
            else:
                self._set(mint, w, "long", f"Jev {j['score']} na startu – slabý", c)
        elif w["state"] == "long" and w.get("long_jev_check"):
            w["long_jev_check"] = False
            if j["score"] >= cfg["jev_good"]:
                self._set(mint, w, "short", f"zlepšil se a Jev {j['score']}", c)
            else:
                self._set(mint, w, "archived", f"zlepšil se, ale Jev {j['score']} špatný", c)

    def on_ai(self, mint: str, ai: dict | None, gid: int | None):
        w = self.st.get(mint)
        if not w:
            return
        with self.s.lock:
            c = self.s.coins.get(mint)
        w["gid"] = gid or w.get("gid")
        if not ai:
            return
        if ai["verdict"] == "BUY":
            self._set(mint, w, "gem", f"AI BUY {ai['confidence'] * 100:.0f} %", c)
        elif ai["verdict"] == "SKIP":
            self._set(mint, w, "long", "AI řekla SKIP", c)

    def on_gem_exit(self, mint: str, reason: str):
        w = self.st.get(mint)
        if w and w["state"] == "gem":
            with self.s.lock:
                c = self.s.coins.get(mint)
            self._set(mint, w, "short", reason, c)

    # ------------------------------------------------------------------ hlavní krok (každé ~2 s)
    def step(self, scored: list, all_coins: dict):
        cfg = self.cfg
        g = self.s.cfg.get("gems", {})
        watch, buy = g.get("watch_score", 50), g.get("buy_score", 65)
        now = time.time()
        seen = set()
        for sc, why, c in scored:
            mint = c["mint"]
            seen.add(mint)
            w = self.st.get(mint)
            if w is None:
                w = {"symbol": c.get("symbol"), "name": c.get("name"), "created_ms": c.get("created_ms"),
                     "first_ts": now, "entry": sc, "last": sc, "best": sc, "peak_mcap": c.get("mcap") or 0,
                     "state": None, "jev_n": 0}
                self.st[mint] = w
                if sc >= watch:
                    self._set(mint, w, "short", f"skóre {sc}", c)
                else:
                    self._set(mint, w, "shit", f"skóre {sc}", c)
                    if not self._jev(mint, c, sc, why, "low"):
                        self._set(mint, w, "long", "bez Jevu (limit/klíč)", c)
            if w["state"] == "archived":
                continue
            w["last"] = sc
            w["best"] = max(w.get("best", 0), sc)
            w["peak_mcap"] = max(w.get("peak_mcap") or 0, c.get("mcap") or 0)
            w["why"], w["c"] = why, c
            st = w["state"]
            if st == "long":
                self._step_long(mint, w, c, sc, why, cfg, now)
            elif st == "short":
                self._step_short(mint, w, c, sc, why, cfg, now, buy, g)
            elif st == "gem":
                if (c.get("mcap") or 0) <= w["peak_mcap"] * (1 - cfg["gem_exit_drop_pct"] / 100):
                    self.on_gem_exit(mint, f"MC −{cfg['gem_exit_drop_pct']} % z vrcholu")
        # coiny, které z gem skóre vypadly (bot, filtry, zmizely z Pulse)
        for mint, w in list(self.st.items()):
            if mint in seen or w["state"] in ("archived", "gem"):
                continue
            c = all_coins.get(mint)
            if c is None:
                if w["state"] in ("long", "short", "shit"):
                    self._set(mint, w, "archived", "vypadl z Pulse", w.get("c"))
            elif (c.get("pat") or {}).get("verdict") == "bot":
                self._set(mint, w, "archived", "bot filtr", c)
        # top N na long watch – nejslabší vypadnou
        longs = [(w["last"], m) for m, w in self.st.items() if w["state"] == "long"]
        if len(longs) > cfg["long_max"]:
            for _, m in sorted(longs)[: len(longs) - cfg["long_max"]]:
                self._set(m, self.st[m], "archived", f"vytlačen z top {cfg['long_max']}", self.st[m].get("c"))
        # úklid paměti – archivované starší než 1 h
        for m in [m for m, w in self.st.items() if w["state"] == "archived" and now - w["since"] > 3600]:
            self.st.pop(m, None)

    def _step_long(self, mint, w, c, sc, why, cfg, now):
        if now - w.get("last_eval", 0) < cfg["long_check_sec"]:
            return
        w["last_eval"] = now
        if now - w["since"] > cfg["long_max_min"] * 60:
            return self._set(mint, w, "archived", f"{cfg['long_max_min']} min na long watch bez zlepšení", c)
        if (c.get("mcap") or 0) <= w["peak_mcap"] * (1 - cfg["long_mc_drop_pct"] / 100):
            return self._set(mint, w, "archived", f"MC −{cfg['long_mc_drop_pct']} % z vrcholu", c)
        if sc <= w["entry"] - cfg["long_drop_pts"]:
            w["fail"] = w.get("fail", 0) + 1
            if w["fail"] >= cfg["confirm_checks"]:
                return self._set(mint, w, "archived", f"skóre −{w['entry'] - sc} bodů", c)
        else:
            w["fail"] = 0
        # zlepšení oproti vstupu -> Jev se podívá
        if sc >= w["entry"] + cfg["long_improve_pts"] and not w.get("long_jev_check"):
            w["long_jev_check"] = True
            if not self._jev(mint, c, sc, why, "low"):
                w["long_jev_check"] = False
                if sc >= self.s.cfg.get("gems", {}).get("watch_score", 50):
                    self._set(mint, w, "short", f"zlepšil se na {sc} (bez Jevu)", c)

    def _step_short(self, mint, w, c, sc, why, cfg, now, buy, g):
        if now - w.get("last_eval", 0) < cfg["short_cycle_sec"]:
            return
        w["last_eval"] = now
        w["cycles"] = w.get("cycles", 0) + 1
        if w["cycles"] % max(1, cfg["short_jev_every"]) == 0:
            self._jev(mint, c, sc, why, "high")
        # zhoršení -> long
        jev_drop = (w.get("jev_best") or 0) - (w.get("jev_last") or w.get("jev_best") or 0)
        if sc <= w["entry"] - cfg["short_drop_pts"] or jev_drop >= cfg["short_jev_drop"]:
            w["fail"] = w.get("fail", 0) + 1
            if w["fail"] >= cfg["confirm_checks"]:
                why_txt = f"Jev −{jev_drop}" if jev_drop >= cfg["short_jev_drop"] else f"skóre −{w['entry'] - sc} bodů"
                return self._set(mint, w, "long", why_txt, c)
        else:
            w["fail"] = 0
        if now - w["since"] > cfg["short_max_min"] * 60 and sc < buy:
            return self._set(mint, w, "long", f"{cfg['short_max_min']} min na short watch bez BUY", c)
        # povýšení -> AI report
        if sc >= buy:
            jl = w.get("jev_last")
            if self.s.has_jev() and jl is None:
                self._jev(mint, c, sc, why, "high")
                return
            if jl is not None and jl < g.get("report_min_jev", 50):
                return
            self.s.request_report(sc, why, c)


DEFAULTS = {
    "long_max": 200, "long_max_min": 15, "long_check_sec": 20, "long_improve_pts": 10, "long_drop_pts": 15,
    "long_mc_drop_pct": 50, "confirm_checks": 2, "jev_good": 55,
    "short_cycle_sec": 30, "short_jev_every": 2, "short_drop_pts": 10, "short_jev_drop": 25, "short_max_min": 30,
    "gem_exit_drop_pct": 40, "jev_low_share": 70,
}


# ------------------------------------------------------------------ Pull archiv (bez AI)
def pull_archive(hours: float = 24, limit: int = 400, log=lambda l, m: None) -> dict:
    """Pro archivované coiny zjistí, co udělaly PO vyhození (aktuální MC + ATH po archivaci). Bez AI."""
    from . import pumpfun
    rows = [dict(r) for r in db.query(
        "SELECT * FROM watch_archive WHERE archived_ts > ? ORDER BY archived_ts DESC LIMIT ?",
        (time.time() - hours * 3600, limit))]
    n = 0
    for r in rows:
        try:
            c = pumpfun.coin(r["mint"])
        except Exception:  # noqa: BLE001
            c = None
        if not c or not r["mcap_at"]:
            continue
        peak = max(r.get("peak_after") or 0, c["mcap"])
        if c.get("ath_ms") and c["ath_ms"] / 1000 >= r["archived_ts"]:
            peak = max(peak, c.get("ath_mcap") or 0)
        with db.connect() as con:
            con.execute("UPDATE watch_archive SET pulled_ts=?, mcap_now=?, peak_after=?, mult_after=?, mult_now=? "
                        "WHERE mint=?", (time.time(), c["mcap"], peak, peak / r["mcap_at"], c["mcap"] / r["mcap_at"],
                                         r["mint"]))
        n += 1
        time.sleep(0.15)
    return archive_summary(hours) | {"pulled": n}


def archive_summary(hours: float = 24) -> dict:
    rows = [dict(r) for r in db.query("SELECT * FROM watch_archive WHERE archived_ts > ?", (time.time() - hours * 3600,))]
    pulled = [r for r in rows if r.get("mult_after") is not None]
    tiers = {t: sum(1 for r in pulled if r["mult_after"] >= t) for t in (2, 5, 10, 20)}
    by_reason: dict[str, dict] = {}
    for r in pulled:
        k = (r["reason"] or "?").split(" (")[0]
        for p in ("skóre −", "MC −", "vytlačen z top", "zlepšil se, ale Jev", "Jev "):
            if k.startswith(p):
                k = p.strip() + "…"
                break
        d = by_reason.setdefault(k, {"n": 0, "x2": 0, "x5": 0, "x10": 0, "best": 0.0})
        d["n"] += 1
        d["x2"] += r["mult_after"] >= 2
        d["x5"] += r["mult_after"] >= 5
        d["x10"] += r["mult_after"] >= 10
        d["best"] = max(d["best"], r["mult_after"])
    misses = sorted([r for r in pulled if r["mult_after"] >= 2], key=lambda r: -r["mult_after"])[:50]
    return {"total": len(rows), "pulled_n": len(pulled), "tiers": tiers, "by_reason": by_reason, "misses": misses}
