"""Gem detektor – hledá coiny s potenciálem na velký pohyb (cíl 10–20×).

1) ZDARMA: gem skóre 0–100 z dat, která už Pulse má (momentum, aktivita, rychlost bonding curve,
   vzdálenost od ATH, komunita, prostor k růstu, bezpečnost, bot filtr).
2) Nad prahem -> AI report se strategií (OpenRouter, max N/hod).
3) BUY -> popup + uložení do DB (tabulka gems) a sledování 2× / 5× / 10× / 20×."""
import json
import time

from . import db, pumpfun

TIERS = (20, 10, 5, 2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS gems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL, symbol TEXT, name TEXT, source TEXT,
    ts REAL NOT NULL, mcap0 REAL, progress0 REAL, complete0 INTEGER,
    gem_score INTEGER, gem_reasons TEXT,
    verdict TEXT, confidence REAL, safety TEXT, potential TEXT,
    summary TEXT, thesis TEXT, narrative TEXT, red_flags TEXT, strategy TEXT, model TEXT,
    risk_label TEXT, bot_score INTEGER, popped INTEGER DEFAULT 0,
    last_mcap REAL, peak_mcap REAL, peak_mult REAL, last_mult REAL, peak_ts REAL,
    last_checked REAL, outcome TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_gems_ts ON gems(ts);
"""

SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, created_ms REAL,
    first_seen REAL, first_mcap REAL, first_progress REAL,
    last_seen REAL, last_mcap REAL,
    max_score INTEGER, max_score_ts REAL, mcap_at_max_score REAL,
    first_hot_ts REAL, mcap_at_hot REAL,
    last_reason TEXT, was_bot INTEGER DEFAULT 0, was_candidate INTEGER DEFAULT 0,
    injected INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seen_last ON seen(last_seen);
"""

# sloupce přidané v novějších verzích (migrace existující DB)
EXTRA_COLS = {"stage": "TEXT", "screen": "TEXT", "cost": "REAL", "ai_tries": "INTEGER DEFAULT 0",
              "last_ai_try": "REAL", "stop_reason": "TEXT", "paid": "INTEGER",
              "jev_score": "INTEGER", "jev_action": "TEXT", "jev_reason": "TEXT",
              "cycles": "INTEGER DEFAULT 0", "ai_updates": "INTEGER DEFAULT 0", "jev_updates": "INTEGER DEFAULT 0",
              "archived": "INTEGER DEFAULT 0", "last_action": "TEXT", "last_update_text": "TEXT", "jev0": "INTEGER"}

UPDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS gem_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER NOT NULL, ts REAL, cycle INTEGER,
    kind TEXT,              -- price | jev | ai
    mcap REAL, mult REAL, jev_score INTEGER, action TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_gu_gid ON gem_updates(gid);
"""


def migrate(con):
    con.executescript(SEEN_SCHEMA)
    con.executescript(UPDATES_SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(gems)")}
    for col, typ in EXTRA_COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE gems ADD COLUMN {col} {typ}")


def init():
    with db.connect() as con:
        con.executescript(SCHEMA)
        migrate(con)


# ------------------------------------------------------------------ skóre
def score(c: dict, hist: list[tuple[float, float]], cfg: dict) -> tuple[int, list[str]]:
    """Vrátí (skóre, důvody). Nic nevolá – jen počítá z dat v paměti."""
    g = cfg.get("gems", {})
    W = {"momentum": 1.0, "activity": 1.0, "volume": 1.0, "bonding": 1.0, "ath": 1.0, "community": 1.0,
         "room": 1.0, "safety": 1.0, **(g.get("weights") or {})}
    parts = {k: 0.0 for k in W}
    why: list[str] = []
    mcap = c.get("mcap") or 0
    now = time.time()
    pat = c.get("pat") or {}
    m = pat.get("m") or {}

# 1) momentum – růst MC za posledních ~5 min (0–25)
    past = [v for t, v in hist if now - 330 <= t <= now - 240]
    base = past[0] if past else (hist[0][1] if hist and now - hist[0][0] > 90 else None)
    chg = (mcap / base - 1) if base else m.get("chg")
    if chg is not None:
        if chg >= 0.4:
            parts["momentum"] += 25
            why.append(f"MC +{chg * 100:.0f} % za 5 min")
        elif chg >= 0.2:
            parts["momentum"] += 16
            why.append(f"MC +{chg * 100:.0f} % za 5 min")
        elif chg >= 0.08:
            parts["momentum"] += 8
        elif chg <= -0.25:
            parts["momentum"] -= 10
            why.append(f"padá ({chg * 100:.0f} %)")

    # 2) aktivita – kolik sekund z posledních 10 min mělo obchod (0–20)
    act = m.get("active_sec") or 0
    if act >= 150:
        parts["activity"] += 20
        why.append(f"hodně obchodů ({act} s aktivních / 10 min)")
    elif act >= 80:
        parts["activity"] += 14
    elif act >= 40:
        parts["activity"] += 7

    # 3) objem zrychluje (0–10)
    vt = m.get("vol_trend")
    if vt and vt >= 2:
        parts["volume"] += 10
        why.append(f"objem zrychluje ({vt:.1f}×)")
    elif vt and vt >= 1.3:
        parts["volume"] += 5

    # 4) rychlost bonding curve / čerstvá migrace (0–15)
    if not c.get("complete"):
        speed = c.get("progress", 0) / max(c.get("age_min", 1), 1)
        if speed >= 8:
            parts["bonding"] += 15
            why.append(f"bonding letí ({speed:.0f} %/min)")
        elif speed >= 3:
            parts["bonding"] += 10
            why.append(f"rychlý bonding ({speed:.1f} %/min)")
        elif speed >= 1:
            parts["bonding"] += 5
    elif c.get("migrated_at") and now - c["migrated_at"] < 1800:
        parts["bonding"] += 10
        why.append("čerstvě migroval a drží se")

    # 5) blízko ATH = nikdo nedumpnul (0–10)
    ath = c.get("ath_mcap") or 0
    if ath:
        r = mcap / ath
        if r >= 0.85:
            parts["ath"] += 10
            why.append("u ATH")
        elif r >= 0.6:
            parts["ath"] += 5
        elif r < 0.35:
            parts["ath"] -= 10
            why.append(f"{(1 - r) * 100:.0f} % pod ATH")

    # 6) komunita (0–12)
    soc = sum(1 for k in ("twitter", "telegram", "website") if c.get(k))
    parts["community"] += {0: 0, 1: 3}.get(soc, 6)
    if soc >= 2:
        why.append(f"{soc} socials")
    rep = c.get("replies") or 0
    parts["community"] += 4 if rep >= 20 else 2 if rep >= 5 else 0
    if c.get("is_live"):
        parts["community"] += 2
        why.append("live stream")

    # 7) prostor k růstu – malý MC (0–8)
    lo, hi = g.get("sweet_mcap_min", 8000), g.get("sweet_mcap_max", 150_000)
    if lo <= mcap <= hi:
        parts["room"] += 8
    elif mcap <= hi * 3:
        parts["room"] += 4

    # 8) bezpečnost
    rl = c.get("risk_label")
    rc = c.get("rc") or {}
    if rl == "LOW":
        parts["safety"] += 5
    elif rl == "HIGH":
        parts["safety"] -= 30
        why.append("HIGH risk")
    if rc and rc.get("top10_pct", 0) > 35:
        parts["safety"] -= 10
        why.append(f"top10 drží {rc['top10_pct']:.0f} %")
    if rc and (rc.get("mint_authority") or rc.get("freeze_authority")):
        parts["safety"] -= 40
        why.append("mint/freeze authority!")
    pts = sum(v * W[k] for k, v in parts.items())
    return int(max(0, min(100, pts))), why


def is_candidate(c: dict, cfg: dict) -> bool:
    g = cfg.get("gems", {})
    pat = (c.get("pat") or {}).get("verdict")
    if pat == "bot":
        return False
    if g.get("require_bot_check", True) and pat != "ok":
        return False
    mc = c.get("mcap") or 0
    if mc < g.get("min_mcap", 5000) or (g.get("max_mcap") and mc > g["max_mcap"]):
        return False
    return c.get("age_min", 0) >= g.get("min_age_min", 1)


# ------------------------------------------------------------------ DB
def _ai_fields(ai: dict | None) -> dict:
    ai = ai or {}
    return {
        "verdict": ai.get("verdict"), "confidence": ai.get("confidence"), "safety": ai.get("safety"),
        "potential": ai.get("potential"), "summary": ai.get("summary"), "thesis": ai.get("thesis"),
        "narrative": ai.get("narrative"), "red_flags": json.dumps(ai.get("red_flags") or [], ensure_ascii=False),
        "strategy": json.dumps(ai.get("strategy") or {}, ensure_ascii=False), "model": ai.get("model"),
        "paid": int(bool(ai.get("paid"))) if ai else None,
    }


def update_ai(gid: int, ai: dict | None, stage: str, screen: dict | None, popped: bool):
    f = _ai_fields(ai)
    f.update(stage=stage, screen=json.dumps(screen or {}, ensure_ascii=False), last_ai_try=time.time())
    with db.connect() as con:
        cost = (ai or {}).get("cost", 0) or 0
        con.execute(f"UPDATE gems SET {','.join(k + '=?' for k in f)}, ai_tries = COALESCE(ai_tries,0) + 1, "
                    f"cost = COALESCE(cost,0) + ?, popped = MAX(popped, ?) WHERE id=?",
                    [*f.values(), cost, int(popped), gid])


def update_jev(gid: int, jev: dict):
    with db.connect() as con:
        con.execute("UPDATE gems SET jev_score=?, jev_action=?, jev_reason=?, cost=COALESCE(cost,0)+? WHERE id=?",
                    (jev.get("score"), jev.get("hint"), jev.get("reason"), jev.get("cost") or 0, gid))


def save(c: dict, gem_score: int, reasons: list[str], ai: dict | None, source: str, popped: bool,
         stage: str = "report", screen: dict | None = None, jev: dict | None = None) -> int:
    ai = ai or {}
    jev = jev or {}
    row = {
        "address": c["mint"], "symbol": c.get("symbol"), "name": c.get("name"), "source": source,
        "ts": time.time(), "mcap0": c.get("mcap"), "progress0": c.get("progress"),
        "complete0": int(bool(c.get("complete"))), "gem_score": gem_score,
        "gem_reasons": json.dumps(reasons, ensure_ascii=False),
        "verdict": ai.get("verdict"), "confidence": ai.get("confidence"), "safety": ai.get("safety"),
        "potential": ai.get("potential"), "summary": ai.get("summary"), "thesis": ai.get("thesis"),
        "narrative": ai.get("narrative"), "red_flags": json.dumps(ai.get("red_flags") or [], ensure_ascii=False),
        "strategy": json.dumps(ai.get("strategy") or {}, ensure_ascii=False), "model": ai.get("model"),
        "risk_label": c.get("risk_label"), "bot_score": (c.get("pat") or {}).get("score"),
        "popped": int(popped), "last_mcap": c.get("mcap"), "peak_mcap": c.get("mcap"),
        "peak_mult": 1.0, "last_mult": 1.0, "peak_ts": time.time(),
        "stage": stage, "screen": json.dumps(screen or {}, ensure_ascii=False),
        "cost": (ai.get("cost") or 0) + ((screen or {}).get("cost") or 0),
        "ai_tries": 1 if stage in ("report", "no_ai") else 0, "last_ai_try": time.time(),
        "paid": int(bool(ai.get("paid"))) if ai else None,
        "jev_score": jev.get("score"), "jev_action": jev.get("hint"), "jev_reason": jev.get("reason"),
        "jev0": jev.get("score"),
    }
    row["cost"] = (row["cost"] or 0) + (jev.get("cost") or 0)
    with db.connect() as con:
        cur = con.execute(f"INSERT INTO gems ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                          list(row.values()))
        return cur.lastrowid


def list_gems(limit: int = 300, only_buy: bool = False) -> list[dict]:
    q = "SELECT * FROM gems" + (" WHERE verdict='BUY'" if only_buy else "") + " ORDER BY ts DESC LIMIT ?"
    return [dict(r) for r in db.query(q, (limit,))]


def price_fields(r: dict, c: dict, now: float, dur: float) -> dict:
    """Nová cena gemu -> pole pro UPDATE (peak bere i z pump.fun ATH, nic neuteče mezi kontrolami)."""
    peak = max(r["peak_mcap"] or 0, c["mcap"])
    peak_ts = r["peak_ts"]
    if c.get("ath_ms") and c["ath_ms"] / 1000 >= r["ts"] and c["ath_mcap"] > peak:
        peak, peak_ts = c["ath_mcap"], c["ath_ms"] / 1000
    elif c["mcap"] >= peak:
        peak_ts = now
    pm, lm = peak / r["mcap0"], c["mcap"] / r["mcap0"]
    f = {"last_mcap": c["mcap"], "peak_mcap": peak, "peak_mult": pm, "last_mult": lm,
         "peak_ts": peak_ts, "last_checked": now}
    best = next((f"{t}x" for t in TIERS if pm >= t), None)
    if lm < 0.2 or c["mcap"] < 2000:
        f.update(outcome=best or "rug", stop_reason="spadl na nulu")
    elif now - r["ts"] > dur:
        f.update(outcome=best or "miss", stop_reason="konec sledování")
    return f


def update_row(gid: int, f: dict):
    with db.connect() as con:
        con.execute(f"UPDATE gems SET {','.join(k + '=?' for k in f)} WHERE id=?", [*f.values(), gid])


def add_update(gid: int, cycle: int, kind: str, mcap=None, mult=None, jev_score=None, action=None, data=None):
    with db.connect() as con:
        con.execute("INSERT INTO gem_updates (gid, ts, cycle, kind, mcap, mult, jev_score, action, data) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (gid, time.time(), cycle, kind, mcap, mult, jev_score, action,
                     json.dumps(data, ensure_ascii=False) if data is not None else None))


def get_updates(gid: int, kinds=("jev", "ai")) -> list[dict]:
    q = f"SELECT * FROM gem_updates WHERE gid=? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY ts"
    return [dict(r) for r in db.query(q, (gid, *kinds))]


def in_deep_mode(r: dict, cfg: dict) -> bool:
    b = cfg.get("buy_tracking", {})
    return (r["verdict"] == "BUY" and b.get("enabled", True) and not r["archived"]
            and (r["cycles"] or 0) < b.get("deep_cycles", 120))


def track_once(cfg: dict, retry=None) -> int:
    """Běžné sledování (BUY v hlubokém režimu řeší buy_tracker v Pulse každých 30 s):
    - BUY po hlubokém režimu: každou 1 min až do konce (24 h)
    - ne-BUY gemy (WATCH / Jev / SKIP): po X min (default 5) do archivu (logu) – dál se nesledují živě,
      jen jedna závěrečná kontrola na konci (peak přes pump.fun ATH), aby šla měřit úspěšnost
    - bez AI na buy skóre: každých 5 min znovu na AI, max 3×
    - spadne na nulu: stop jako rug"""
    g = cfg.get("gems", {})
    dur = cfg.get("tracking", {}).get("duration_hours", 24) * 3600
    fast = g.get("track_fast_sec", 60)
    archive_after = g.get("nonbuy_archive_min", 5) * 60
    retry_every, max_tries = g.get("ai_retry_min", 5) * 60, g.get("ai_max_tries", 3)
    now = time.time()
    rows = [dict(r) for r in db.query("SELECT * FROM gems WHERE outcome='pending'")]
    n = 0
    retries = 0
    for r in rows:
        v = r["verdict"]
        age = now - r["ts"]
        if in_deep_mode(r, cfg):
            continue
        waiting_ai = not v and r["stage"] == "no_ai" and (r["ai_tries"] or 0) < max_tries \
            and age <= retry_every * (max_tries + 1)
        if waiting_ai and retry and retries < 3 and now - (r["last_ai_try"] or r["ts"]) >= retry_every:
            retries += 1
            retry(r["id"], r["address"])
        if r["archived"] and age < dur:
            continue              # staré archivované (dřívější verze): jen závěrečná kontrola
        if v == "SKIP" and age > archive_after:
            if not r["archived"]:
                update_row(r["id"], {"archived": 1, "stop_reason": "AI SKIP – archiv"})
            continue
        if r["last_checked"] and now - r["last_checked"] < fast - 5:
            continue
        try:
            c = pumpfun.coin(r["address"])
        except Exception:  # noqa: BLE001
            continue
        if not c or not r["mcap0"]:
            continue
        f = price_fields(r, c, now, dur)
        if r["archived"] and "outcome" not in f:
            pm = f["peak_mult"]
            f.update(outcome=next((f"{t}x" for t in TIERS if pm >= t), "miss"), stop_reason="archiv – konec")
        if not v and r["stage"] == "no_ai" and not waiting_ai and "outcome" not in f:
            f.update(outcome=next((f"{t}x" for t in TIERS if f["peak_mult"] >= t), "no_ai"),
                     stop_reason="AI nešla ani po opakování")
        update_row(r["id"], f)
        n += 1
        time.sleep(0.2)
    return n


def tier(mult: float | None) -> str:
    if not mult:
        return ""
    for t in TIERS:
        if mult >= t:
            return f"{t}×+"
    return ""


def summary() -> dict:
    rows = db.query("SELECT verdict, peak_mult, outcome, ts, cost FROM gems")
    out = {"total": len(rows), "buy": 0, "tiers": {}, "buy_tiers": {}, "evaluated": 0, "buy_evaluated": 0}
    for t in TIERS:
        out["tiers"][t] = sum(1 for r in rows if (r["peak_mult"] or 0) >= t)
        out["buy_tiers"][t] = sum(1 for r in rows if r["verdict"] == "BUY" and (r["peak_mult"] or 0) >= t)
    out["buy"] = sum(1 for r in rows if r["verdict"] == "BUY")
    out["evaluated"] = sum(1 for r in rows if r["outcome"] != "pending")
    out["buy_evaluated"] = sum(1 for r in rows if r["verdict"] == "BUY" and r["outcome"] != "pending")
    out["rugs"] = sum(1 for r in rows if r["outcome"] == "rug")
    out["watch"] = sum(1 for r in rows if r["verdict"] == "WATCH")
    out["cost"] = sum(r["cost"] or 0 for r in rows)
    return out


# ------------------------------------------------------------------ vše, co Pulse viděl
def record_seen(rows: list[dict]):
    """Upsert každého coinu, který Pulse viděl, a proč (ne)byl gem. Pro zpětnou analýzu runnerů."""
    if not rows:
        return
    now = time.time()
    data = [(r["mint"], r.get("symbol"), r.get("name"), r.get("created_ms"), now, r.get("mcap"), r.get("progress"),
             now, r.get("mcap"), r.get("score"), now if r.get("score") is not None else None,
             r.get("mcap") if r.get("score") is not None else None,
             now if r.get("hot") else None, r.get("mcap") if r.get("hot") else None,
             r.get("reason"), int(bool(r.get("bot"))), int(bool(r.get("candidate"))), int(bool(r.get("injected"))))
            for r in rows]
    with db.connect() as con:
        con.executemany("""
            INSERT INTO seen (mint, symbol, name, created_ms, first_seen, first_mcap, first_progress, last_seen, last_mcap,
                              max_score, max_score_ts, mcap_at_max_score, first_hot_ts, mcap_at_hot,
                              last_reason, was_bot, was_candidate, injected)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mint) DO UPDATE SET
                last_seen = excluded.last_seen, last_mcap = excluded.last_mcap,
                max_score_ts = CASE WHEN COALESCE(excluded.max_score,-1) > COALESCE(max_score,-1)
                                    THEN excluded.max_score_ts ELSE max_score_ts END,
                mcap_at_max_score = CASE WHEN COALESCE(excluded.max_score,-1) > COALESCE(max_score,-1)
                                    THEN excluded.mcap_at_max_score ELSE mcap_at_max_score END,
                max_score = MAX(COALESCE(max_score,-1), COALESCE(excluded.max_score,-1)),
                first_hot_ts = COALESCE(first_hot_ts, excluded.first_hot_ts),
                mcap_at_hot = COALESCE(mcap_at_hot, excluded.mcap_at_hot),
                last_reason = excluded.last_reason,
                was_bot = MAX(was_bot, excluded.was_bot),
                was_candidate = MAX(was_candidate, excluded.was_candidate),
                injected = MAX(injected, excluded.injected)
        """, data)
        con.execute("DELETE FROM seen WHERE last_seen < ?", (now - 3 * 86400,))
