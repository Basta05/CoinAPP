"""Risk rating 0–100 (víc = rizikovější) + štítek LOW / MEDIUM / HIGH."""


def rate(m: dict, rc: dict | None, ai: dict | None, cfg: dict) -> tuple[int, str]:
    score = 0.0

    # RugCheck (max ~40)
    if rc is None:
        score += 25
    else:
        score += min(float(rc.get("score") or 0), 100) * 0.25
        score += 10 if rc.get("mint_authority") else 0
        score += 10 if rc.get("freeze_authority") else 0
        score += min(rc.get("top10_pct", 0), 60) / 60 * 10
        score += min(rc.get("insiders", 0), 5) * 2

    # Trh (max ~30)
    liq = m.get("liquidity") or 0
    score += 15 if liq < 10_000 else 8 if liq < 30_000 else 3 if liq < 80_000 else 0
    ratio = m.get("buys_h1", 0) / max(m.get("sells_h1", 0), 1)
    score += 10 if ratio < 0.8 else 5 if ratio < 1.2 else 0
    age = m.get("age_min") or 0
    score += 5 if age < 15 else 0

    # LLM (max ~30)
    if ai:
        score += {"BUY": 0, "WATCH": 12, "SKIP": 25}.get(ai["verdict"], 15)
        score += (1 - ai["confidence"]) * 5
        score += min(len(ai.get("red_flags") or []), 4) * 2

    score = int(round(min(score, 100)))
    r = cfg.get("risk", {})
    label = "LOW" if score <= r.get("low_max", 35) else "MEDIUM" if score <= r.get("medium_max", 65) else "HIGH"
    return score, label


def quick(rc: dict | None, cfg: dict) -> tuple[int | None, str]:
    """Rychlý rating jen z RugChecku (pro Pulse, bez LLM)."""
    if rc is None:
        return None, "?"
    if rc.get("rugged"):
        return 100, "HIGH"
    score = min(float(rc.get("score") or 0), 100) * 0.4
    score += 20 if rc.get("mint_authority") else 0
    score += 20 if rc.get("freeze_authority") else 0
    score += min(rc.get("top10_pct", 0), 60) / 60 * 25
    score += min(rc.get("insiders", 0), 5) * 3
    score += 15 if rc.get("has_danger") else 0
    score = int(round(min(score, 100)))
    r = cfg.get("risk", {})
    label = "LOW" if score <= r.get("low_max", 35) else "MEDIUM" if score <= r.get("medium_max", 65) else "HIGH"
    return score, label
