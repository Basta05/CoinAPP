"""🏁 Runneři – zpětná kontrola: které velké coiny letěly, jestli jsme je chytili, a proč ne.

Každých X min:
 1) najde velké migrované coiny z posledních 24 h (MC nebo ATH nad hranicí)
 2) pro každý dohledá, co o něm appka věděla (tabulka seen + gems): kdy ho viděla, při jakém MC,
    jaké měl gem skóre, proč ho vyřadila (bot / filtr sloupce / nízké skóre / AI SKIP / vůbec neviděla)
 3) z 1m svíček udělá časovou osu (kdy dal 50k, 100k, 300k, 1M…)
 4) AI napíše post-mortem: proč jsme ho minuli + konkrétní úpravy nastavení (dají se použít jedním klikem)"""
import json
import time

from . import candles, db, llm, pumpfun
from .config import deep_merge, load_config, save_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT UNIQUE NOT NULL, symbol TEXT, name TEXT,
    detected_ts REAL, created_ms REAL, mcap_now REAL, ath_mcap REAL,
    status TEXT, status_text TEXT,
    first_seen REAL, first_mcap REAL, first_hot_ts REAL, mcap_at_hot REAL, max_score INTEGER,
    last_reason TEXT, gem_id INTEGER, gem_verdict TEXT, gem_mcap REAL, potential_mult REAL,
    timeline TEXT, ai_why TEXT, ai_lesson TEXT, catchable INTEGER, report_id INTEGER
);
CREATE TABLE IF NOT EXISTS runner_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, n_runners INTEGER, summary TEXT,
    recommendations TEXT, notes TEXT, model TEXT, cost REAL, applied TEXT DEFAULT '[]'
);
"""

STATUS = {
    "caught_buy": ("✅ chycen – AI BUY", "#22c55e"),
    "caught_watch": ("👀 chycen – AI WATCH", "#eab308"),
    "ai_skip": ("🤖 AI / screen řekla SKIP", "#f97316"),
    "risk_skip": ("🛡 vyřazen jako rizikový", "#f97316"),
    "no_ai": ("⚠ gem, ale AI nešla", "#f97316"),
    "low_score": ("📉 viděn, gem skóre pod prahem", "#ef4444"),
    "bot": ("🤖 bot filtr ho vyřadil", "#ef4444"),
    "filtered": ("🚫 vyřazen filtrem", "#ef4444"),
    "not_seen": ("❌ appka ho vůbec neviděla", "#9ca3af"),
}

# co smí AI doporučit měnit (a v jakých mezích) – nic jiného se nepoužije
ALLOWED = {
    "gems.watch_score": ("int", 20, 95),
    "gems.buy_score": ("int", 30, 99),
    "gems.report_min_jev": ("int", 0, 100),
    "gems.min_mcap": ("int", 0, 500_000),
    "gems.max_mcap": ("int", 50_000, 50_000_000),
    "gems.min_age_min": ("float", 0, 120),
    "gems.sweet_mcap_min": ("int", 0, 1_000_000),
    "gems.sweet_mcap_max": ("int", 10_000, 10_000_000),
    "gems.from_scanner": ("bool", None, None),
    "bot_filter.sensitivity": ("choice", ["nízká", "střední", "vysoká"], None),
    "bot_filter.min_age_sec": ("int", 10, 600),
    "pulse.soon_progress": ("int", 10, 99),
    "pulse.keep_hours": ("int", 1, 48),
    "safety_filters.max_top10_holders_pct": ("int", 5, 100),
    "safety_filters.max_rugcheck_score": ("int", 0, 100),
}
for _w in ("momentum", "activity", "volume", "bonding", "ath", "community", "room", "safety"):
    ALLOWED[f"gems.weights.{_w}"] = ("float", 0, 3)
for _c in ("new", "soon", "migrated"):
    ALLOWED[f"pulse.columns.{_c}.min_mcap"] = ("int", 0, 1_000_000)
    ALLOWED[f"pulse.columns.{_c}.max_mcap"] = ("int", 0, 100_000_000)
    ALLOWED[f"pulse.columns.{_c}.max_age_min"] = ("int", 0, 100_000)
    ALLOWED[f"pulse.columns.{_c}.min_progress"] = ("int", 0, 100)
    ALLOWED[f"pulse.columns.{_c}.require_socials"] = ("bool", None, None)
    ALLOWED[f"pulse.columns.{_c}.min_replies"] = ("int", 0, 1000)
    ALLOWED[f"pulse.columns.{_c}.max_risk"] = ("choice", ["LOW", "MEDIUM", "HIGH"], None)

POSTMORTEM_SYSTEM = """Jsi analytik, který ladí automatický detektor Solana memecoin gemů (pump.fun).
Dostaneš seznam coinů, které za posledních 24 h vyletěly (runneři), a ke každému, co o něm detektor věděl:
kdy a při jakém MC ho poprvé viděl, jeho max gem skóre, proč ho vyřadil, verdikt AI, a časovou osu MC.

Jak detektor funguje:
- Pulse sleduje pump.fun coiny ve sloupcích New / Soon migrating / Migrated, každý sloupec má filtry (config pulse.columns.*).
- Bot filtr vyřazuje fake objem (schody, pila, stejné objemy, hodinky, wash trading) – citlivost bot_filter.sensitivity.
- Gem skóre 0–100 = součet složek × váhy (gems.weights.*): momentum (růst MC za 5 min, max 25),
  activity (obchody za 10 min, max 20), volume (zrychlování objemu, max 10), bonding (rychlost bonding curve
  / čerstvá migrace, max 15), ath (blízko ATH, max 10), community (socials, komentáře, max 12),
  room (MC v gems.sweet_mcap_min–max, max 8), safety (RugCheck, bonus/penalizace).
- Skóre ≥ gems.watch_score -> Jev skóre (TypeSafe Jev: scam, potenciál, akce -> 0–100).
- Skóre ≥ gems.buy_score a Jev skóre ≥ gems.report_min_jev -> AI report (free model, placený jen jako pojistka) -> popup při BUY.

Úkol: pro každý coin vysvětli, proč ho detektor minul (nebo chytil pozdě/brzy) a jestli šel realisticky chytit
dřív. Pak navrhni KONKRÉTNÍ změny nastavení, které by takové coiny chytaly častěji, ALE nezaplavily
uživatele slopem (pamatuj, že většina pump.fun coinů umře). Navrhuj opatrně, max 5 změn, jen z povolených cest.
Když je dat málo nebo coiny chytit nešly, klidně nenavrhuj nic.

Odpověz POUZE JSON:
{"summary": "2-4 věty česky – co se stalo a hlavní poučení",
 "coins": [{"mint": "...", "symbol": "...", "why": "proč minut/chycen (1-2 věty)",
            "catchable": true|false, "lesson": "co by pomohlo (1 věta)"}],
 "recommendations": [{"path": "povolená.cesta", "value": hodnota, "why": "proč (1 věta)",
                      "confidence": 0.0-1.0, "risk": "co to může zhoršit (1 věta)"}],
 "notes": "cokoli dalšího (volitelné)"}"""


def init():
    with db.connect() as con:
        con.executescript(SCHEMA)


# ------------------------------------------------------------------ hledání runnerů
def find_runners(cfg: dict, store_coins: list[dict]) -> list[dict]:
    rc = cfg.get("runners", {})
    min_mc = rc.get("min_mcap", 300_000)
    max_age = rc.get("max_age_hours", 24) * 60
    pool: dict[str, dict] = {c["mint"]: c for c in store_coins if c.get("complete")}
    for off in (0, 50, 100):
        try:
            for c in pumpfun.list_coins("last_trade_timestamp", 50, offset=off):
                if c.get("complete"):
                    pool.setdefault(c["mint"], c)
        except Exception:  # noqa: BLE001
            break
    known = {r["mint"] for r in db.query("SELECT mint FROM runners")}
    out = []
    for c in pool.values():
        if c["mint"] in known or c.get("age_min", 1e9) > max_age:
            continue
        big = max(c.get("mcap") or 0, c.get("ath_mcap") or 0)
        if big >= min_mc:
            out.append(c)
    out.sort(key=lambda c: -max(c.get("mcap") or 0, c.get("ath_mcap") or 0))
    return out[: rc.get("max_per_scan", 10)]


def _timeline(c: dict) -> dict:
    """Časová osa MC z 1m svíček: kdy coin překročil 50k / 100k / 300k / 1M a MC v minutách od vzniku."""
    try:
        cs = candles.fetch(c["mint"], "1m", c.get("created_ms"), count=min(1000, int(c.get("age_min", 60)) + 10))
    except Exception:  # noqa: BLE001
        return {}
    if not cs or not c.get("created_ms"):
        return {}
    t0 = c["created_ms"] / 1000
    out = {"cross_min": {}, "mcap_at_min": {}}
    for lvl in (30_000, 50_000, 100_000, 300_000, 1_000_000):
        hit = next((x for x in cs if x[2] >= lvl), None)
        if hit:
            out["cross_min"][f"{lvl // 1000}k"] = round((hit[0] - t0) / 60, 1)
    for m in (5, 15, 30, 60, 120, 240):
        pt = [x for x in cs if x[0] <= t0 + m * 60]
        if pt:
            out["mcap_at_min"][str(m)] = round(pt[-1][4])
    ath = max(cs, key=lambda x: x[2])
    out["ath_at_min"] = round((ath[0] - t0) / 60, 1)
    return out


def _classify(seen: dict | None, gem: dict | None) -> str:
    if gem and gem.get("stage") == "jev" and not gem.get("verdict"):
        return "caught_watch" if gem.get("jev_action") in ("BUY", "WATCH") else "ai_skip"
    if gem:
        v = gem.get("verdict")
        if v == "BUY":
            return "caught_buy"
        if v == "WATCH":
            return "caught_watch"
        if gem.get("stage") == "risk":
            return "risk_skip"
        if v == "SKIP":
            return "ai_skip"
        return "no_ai"
    if not seen:
        return "not_seen"
    if seen.get("was_bot"):
        return "bot"
    if seen.get("was_candidate"):
        return "low_score"
    return "filtered"


def analyze_runner(c: dict) -> dict:
    mint = c["mint"]
    seen = db.query("SELECT * FROM seen WHERE mint=?", (mint,))
    seen = dict(seen[0]) if seen else None
    gem = db.query("SELECT * FROM gems WHERE address=? ORDER BY ts LIMIT 1", (mint,))
    gem = dict(gem[0]) if gem else None
    status = _classify(seen, gem)
    ath = max(c.get("ath_mcap") or 0, c.get("mcap") or 0)
    first_mc = (seen or {}).get("first_mcap") or (gem or {}).get("mcap0")
    return {
        "mint": mint, "symbol": c.get("symbol"), "name": c.get("name"), "detected_ts": time.time(),
        "created_ms": c.get("created_ms"), "mcap_now": c.get("mcap"), "ath_mcap": ath,
        "status": status, "status_text": STATUS[status][0],
        "first_seen": (seen or {}).get("first_seen"), "first_mcap": first_mc,
        "first_hot_ts": (seen or {}).get("first_hot_ts"), "mcap_at_hot": (seen or {}).get("mcap_at_hot"),
        "max_score": (seen or {}).get("max_score") if (seen or {}).get("max_score", -1) >= 0 else None,
        "last_reason": (seen or {}).get("last_reason"),
        "gem_id": (gem or {}).get("id"), "gem_verdict": (gem or {}).get("verdict"), "gem_mcap": (gem or {}).get("mcap0"),
        "potential_mult": (ath / first_mc) if first_mc else None,
        "timeline": json.dumps(_timeline(c), ensure_ascii=False),
        "_gem_summary": (gem or {}).get("summary"), "_jev_score": (gem or {}).get("jev_score"),
        "_jev_action": (gem or {}).get("jev_action"), "_gem_reasons": (gem or {}).get("gem_reasons"),
    }


def save_runner(r: dict) -> int:
    row = {k: v for k, v in r.items() if not k.startswith("_")}
    with db.connect() as con:
        cur = con.execute(f"INSERT OR IGNORE INTO runners ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                          list(row.values()))
        return cur.lastrowid


# ------------------------------------------------------------------ AI post-mortem
def _relevant_cfg(cfg: dict) -> dict:
    g = cfg.get("gems", {})
    return {
        "gems": {k: g.get(k) for k in ("watch_score", "buy_score", "report_min_jev", "min_mcap", "max_mcap", "min_age_min", "sweet_mcap_min",
                                        "sweet_mcap_max", "from_scanner")} | {"weights": g.get("weights") or {}},
        "bot_filter": {k: cfg.get("bot_filter", {}).get(k) for k in ("sensitivity", "min_age_sec")},
        "pulse": {"soon_progress": cfg.get("pulse", {}).get("soon_progress"),
                  "keep_hours": cfg.get("pulse", {}).get("keep_hours"),
                  "columns": cfg.get("pulse", {}).get("columns")},
        "safety_filters": {k: cfg.get("safety_filters", {}).get(k) for k in ("max_top10_holders_pct", "max_rugcheck_score")},
    }


def postmortem(runs: list[dict], cfg: dict) -> dict | None:
    items = []
    for r in runs:
        items.append({
            "mint": r["mint"], "symbol": r["symbol"], "name": r["name"],
            "status": r["status_text"], "mcap_now": r["mcap_now"], "ath_mcap": r["ath_mcap"],
            "age_min": round((time.time() * 1000 - (r["created_ms"] or 0)) / 60000) if r["created_ms"] else None,
            "we_first_saw_min_after_launch": round((r["first_seen"] - r["created_ms"] / 1000) / 60, 1)
            if r.get("first_seen") and r.get("created_ms") else None,
            "we_first_saw_at_mcap": r["first_mcap"], "our_max_gem_score": r["max_score"],
            "score_crossed_threshold_at_mcap": r["mcap_at_hot"], "last_reject_reason": r["last_reason"],
            "gem_ai_verdict": r["gem_verdict"], "jev_score": r.get("_jev_score"), "jev_action": r.get("_jev_action"), "gem_ai_summary": r.get("_gem_summary"),
            "gem_reasons": r.get("_gem_reasons"),
            "possible_multiple_from_first_sight": round(r["potential_mult"], 1) if r.get("potential_mult") else None,
            "timeline": json.loads(r["timeline"] or "{}"),
        })
    stats = db.query("SELECT COUNT(*) n, SUM(verdict='BUY') buy, SUM(peak_mult>=2) x2, AVG(jev_score) avg_jev "
                     "FROM gems WHERE ts > ?",
                     (time.time() - 86400,))
    payload = {"current_config": _relevant_cfg(cfg), "allowed_paths": sorted(ALLOWED),
               "gems_last_24h": dict(stats[0]) if stats else {}, "runners": items}
    msgs = [{"role": "system", "content": POSTMORTEM_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
    # free hlavní, placený pojistka – modely jen z .env
    try:
        ok, why = llm.free_available(cfg)
        if not ok:
            raise RuntimeError(why)
        text, used, cost = llm._call(msgs, 0.2, 2500, llm.free_models(), timeout=120)
    except Exception as e:  # noqa: BLE001
        paid_ok, why_p = llm.paid_available(cfg)
        if not paid_ok:
            raise RuntimeError(f"free: {str(e)[:100]} | placený: {why_p}")
        text, used, cost = llm._call(msgs, 0.2, 2500, llm.paid_models(), timeout=120)
    import re
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).replace("```json", "").replace("```", "")
    m = re.search(r"\{.*\}", t, re.S)
    data = json.loads(m.group(0) if m else t)
    data["model"], data["cost"] = used, cost
    data["recommendations"] = [x for x in (validate(r, cfg) for r in data.get("recommendations") or []) if x]
    return data


def validate(rec: dict, cfg: dict) -> dict | None:
    """Pustí jen povolené cesty a rozumné hodnoty."""
    path = str(rec.get("path", ""))
    if path not in ALLOWED:
        return None
    kind, a, b = ALLOWED[path]
    v = rec.get("value")
    try:
        if kind == "int":
            v = int(round(float(v)))
            v = max(a, min(b, v))
        elif kind == "float":
            v = round(max(a, min(b, float(v))), 2)
        elif kind == "bool":
            v = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "ano", "yes")
        elif kind == "choice":
            if v not in a:
                return None
    except (TypeError, ValueError):
        return None
    cur = cfg
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    if path.startswith("gems.weights.") and cur is None:
        cur = 1.0
    if cur == v:
        return None
    return {**rec, "value": v, "current": cur}


def save_report(data: dict, n: int) -> int:
    with db.connect() as con:
        cur = con.execute("INSERT INTO runner_reports (ts, n_runners, summary, recommendations, notes, model, cost) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (time.time(), n, data.get("summary"), json.dumps(data.get("recommendations") or [], ensure_ascii=False),
                           data.get("notes"), data.get("model"), data.get("cost") or 0))
        rid = cur.lastrowid
        for co in data.get("coins") or []:
            con.execute("UPDATE runners SET ai_why=?, ai_lesson=?, catchable=?, report_id=? WHERE mint=?",
                        (co.get("why"), co.get("lesson"), int(bool(co.get("catchable"))), rid, co.get("mint")))
        return rid


def apply(report_id: int, idx: int) -> str:
    """Použije jedno doporučení -> config.yaml."""
    rows = db.query("SELECT recommendations, applied FROM runner_reports WHERE id=?", (report_id,))
    if not rows:
        return "report nenalezen"
    recs = json.loads(rows[0]["recommendations"] or "[]")
    applied = json.loads(rows[0]["applied"] or "[]")
    if idx >= len(recs):
        return "doporučení nenalezeno"
    cfg = load_config()
    rec = validate(recs[idx], cfg) or recs[idx]
    if recs[idx]["path"] not in ALLOWED:
        return "nepovolená cesta"
    patch: dict = {}
    cur = patch
    parts = rec["path"].split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = rec["value"]
    save_config(deep_merge(cfg, patch))
    applied.append(idx)
    with db.connect() as con:
        con.execute("UPDATE runner_reports SET applied=? WHERE id=?", (json.dumps(sorted(set(applied))), report_id))
    return f"✓ {rec['path']} = {rec['value']}"


# ------------------------------------------------------------------ celý scan
def scan(cfg: dict, store_coins: list[dict], log=lambda l, m: None) -> dict:
    found = find_runners(cfg, store_coins)
    runs = []
    for c in found:
        try:
            r = analyze_runner(c)
            save_runner(r)
            runs.append(r)
        except Exception as e:  # noqa: BLE001
            log("ERROR", f"Runner {c.get('symbol')}: {e}")
    res = {"new": len(runs), "report": None}
    if runs:
        caught = sum(1 for r in runs if r["status"].startswith("caught"))
        log("INFO", f"🏁 Runner scan: {len(runs)} nových runnerů, chyceno {caught}")
    rc = cfg.get("runners", {})
    if runs and rc.get("ai_postmortem", True) and cfg.get("llm", {}).get("enabled", True):
        try:
            data = postmortem(runs, cfg)
            rid = save_report(data, len(runs))
            res["report"] = rid
            log("INFO", f"🏁 AI post-mortem: {data.get('summary', '')[:200]} "
                        f"({len(data.get('recommendations') or [])} doporučení, ${data.get('cost') or 0:.3f})")
        except Exception as e:  # noqa: BLE001
            log("ERROR", f"Runner post-mortem: {e}")
    return res
