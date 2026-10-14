"""Filtry. Každá funkce vrací None (prošlo) nebo text důvodu zamítnutí."""


def ignore(m: dict, cfg: dict) -> str | None:
    """Coiny, které scanner vůbec nebere (neuloží se do DB).
    - starší než max stáří (jen by zaplevelily DB)
    - stale: starší než X hodin a MC nedosahuje (MC za den × počet dní) – mrtvé staré coiny"""
    c = cfg.get("basic_filters", {})
    s = cfg.get("stale_filter", {})
    if (m.get("mcap") or 0) < 1000:
        return "chybná data (MC < $1000)"
    age = m.get("age_min")
    if age is None:
        return None
    if c.get("max_age_minutes") and age > c["max_age_minutes"]:
        return "starý"
    if s.get("enabled", True) and age / 60 > s.get("older_than_hours", 24):
        need = s.get("min_mcap_per_day_usd", 100_000) * (age / 1440)
        if (m.get("mcap") or 0) < need:
            return "stale"
    return None


def basic(m: dict, cfg: dict) -> str | None:
    c = cfg.get("basic_filters", {})
    checks = [
        (m["age_min"] is None, "neznámé stáří páru"),
        # pump.fun bonding curve nemá na DexScreeneru klasickou likviditu -> nekontrolujeme
        (m["dex_id"] != "pumpfun" and (m["liquidity"] or 0) < c.get("min_liquidity_usd", 0),
         f"nízká likvidita (${_n(m['liquidity'])})"),
        ((m["liquidity"] or 0) > c.get("max_liquidity_usd", 1e18), f"vysoká likvidita (${_n(m['liquidity'])})"),
        (c.get("min_mcap_usd", 0) > 0 and (m["mcap"] or 0) < c["min_mcap_usd"],
         f"nízký mcap (${_n(m['mcap'])})"),
        ((m["mcap"] or 0) > c.get("max_mcap_usd", 1e18), f"vysoký mcap (${_n(m['mcap'])})"),
        ((m["volume_h1"] or 0) < c.get("min_volume_h1_usd", 0), f"nízký objem 1h (${_n(m['volume_h1'])})"),
        (m["buys_m5"] < c.get("min_buys_m5", 0), f"málo nákupů 5m ({m['buys_m5']})"),
        (m["buys_h1"] / max(m["sells_h1"], 1) < c.get("min_buy_sell_ratio_h1", 0),
         f"buy/sell 1h {m['buys_h1']}/{m['sells_h1']}"),
        (c.get("require_socials") and not (m["socials"] or m["websites"]), "žádné socials"),
    ]
    for failed, reason in checks:
        if failed:
            return reason
    return None


def safety(rc: dict | None, cfg: dict) -> str | None:
    c = cfg.get("safety_filters", {})
    if not c.get("enabled", True):
        return None
    if rc is None:
        # RugCheck nedostupný / token nezná: nezamítat, pokud to není vyžadováno (AI a risk to zohlední)
        return "RugCheck nedostupný" if c.get("require_rugcheck", False) else None
    if rc["rugged"]:
        return "RugCheck: rugged"
    if c.get("reject_mint_authority") and rc["mint_authority"]:
        return "mint authority aktivní"
    if c.get("reject_freeze_authority") and rc["freeze_authority"]:
        return "freeze authority aktivní"
    if rc["score"] is not None and rc["score"] > c.get("max_rugcheck_score", 1e9):
        return f"RugCheck skóre {rc['score']}"
    if rc["top10_pct"] > c.get("max_top10_holders_pct", 100):
        return f"top10 holderů drží {rc['top10_pct']}%"
    if c.get("reject_danger_risks") and rc["has_danger"]:
        names = ", ".join(r["name"] for r in rc["risks"] if r["level"] == "danger")
        return f"RugCheck danger: {names}"
    return None


def _n(v):
    if v is None:
        return "?"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}k"
    return f"{v:.0f}"


def _age(minutes):
    if minutes < 60:
        return f"{minutes:.0f} min"
    if minutes < 1440:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.0f} dní"
