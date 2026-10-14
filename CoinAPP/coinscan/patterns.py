"""Detekce fake/bot coinů z 1s svíček – zdarma, bez AI a RugChecku.

Hledá vzory, které dělají boti a ne lidi:
  • schody        – skoro jen zelené svíčky, rovná čára nahoru, žádné pullbacky
  • pila          – pravidelné cykly pump → pullback se stejnou velikostí a délkou
  • stejné objemy – obchody pořád stejně velké (bot nakupuje fixní částku)
  • hodinky       – obchody v pravidelných časových rozestupech
  • wash trading  – obrovský objem vůči MC, ale cena se nehýbe
Každý detektor dává body, nad prahem = BOT."""
import math
import statistics as st

SENS = {  # citlivost -> práh skóre pro BOT
    "nízká": 70,
    "střední": 50,
    "vysoká": 35,
}


def _cv(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return None
    m = st.fmean(xs)
    return st.pstdev(xs) / m if m else None


def _r2(ys):
    """R² lineární regrese ys vs index – jak moc je to rovná čára."""
    n = len(ys)
    if n < 3:
        return 0.0
    xs = range(n)
    mx, my = (n - 1) / 2, st.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if not sxx or not syy:
        return 0.0
    return (sxy * sxy) / (sxx * syy)


def _bucket(candles, sec):
    """Sparse 1s svíčky -> uzavírací ceny po `sec` s (mezery doplněné)."""
    if not candles:
        return []
    out = {}
    for t, o, h, l, c, v in candles:
        out[int(t // sec)] = c
    keys = sorted(out)
    closes, last = [], None
    for k in range(keys[0], keys[-1] + 1):
        last = out.get(k, last)
        closes.append(last)
    return closes


def _zigzag(closes, thr):
    """Vrátí swingy [(směr, velikost, délka v krocích)] – pohyby větší než thr (0.04 = 4 %)."""
    if len(closes) < 3:
        return []
    swings = []
    piv, piv_i = closes[0], 0
    ext, ext_i = closes[0], 0
    trend = 0
    for i, p in enumerate(closes):
        if trend == 0:
            if p >= piv * (1 + thr):
                trend, ext, ext_i = 1, p, i
            elif p <= piv * (1 - thr):
                trend, ext, ext_i = -1, p, i
        elif trend == 1:
            if p > ext:
                ext, ext_i = p, i
            elif p <= ext * (1 - thr):
                swings.append((1, ext / piv - 1, ext_i - piv_i))
                piv, piv_i, trend, ext, ext_i = ext, ext_i, -1, p, i
        else:
            if p < ext:
                ext, ext_i = p, i
            elif p >= ext * (1 + thr):
                swings.append((-1, 1 - ext / piv, ext_i - piv_i))
                piv, piv_i, trend, ext, ext_i = ext, ext_i, 1, p, i
    return swings


def analyze(candles: list[list[float]], mcap: float | None = None, sensitivity: str = "střední") -> dict:
    """candles = sparse 1s svíčky [t, o, h, l, c, vol] (jen sekundy s obchodem), ideálně posledních ~10 min."""
    active = [c for c in candles if c[5] and c[5] > 0]
    res = {"verdict": "unknown", "score": 0, "reasons": [], "n": len(active)}
    if len(active) < 12:
        res["reasons"].append(f"málo obchodů ({len(active)})")
        return res
    score = 0
    reasons = []

    # 1) hodinky – pravidelné rozestupy mezi obchody
    gaps = [b[0] - a[0] for a, b in zip(active, active[1:])]
    cv_gap = _cv(gaps)
    mean_gap = st.fmean(gaps) if gaps else 0
    if cv_gap is not None and len(gaps) >= 14 and mean_gap >= 1.5 and cv_gap < 0.2:
        pts = 55 if cv_gap < 0.1 else 30
        score += pts
        reasons.append(f"obchody jako hodinky (každých {mean_gap:.1f}s, rozptyl {cv_gap:.2f})")

    # 2) stejné objemy – bot kupuje pořád stejnou částku
    vols = [c[5] for c in active]
    med = st.median(vols)
    same = sum(1 for v in vols if med and abs(v - med) / med < 0.03) / len(vols)
    cv_vol = _cv(vols)
    if same >= 0.6 and len(vols) >= 15:
        score += 60 if same >= 0.8 else 50
        reasons.append(f"{same * 100:.0f} % obchodů má stejnou velikost (~${med:,.0f})")
    elif same >= 0.35 and len(vols) >= 15:
        score += 25
        reasons.append(f"{same * 100:.0f} % obchodů stejně velkých (volume bot mezi lidmi?)")
    elif cv_vol is not None and cv_vol < 0.15 and len(vols) >= 15:
        score += 30
        reasons.append(f"skoro stejné objemy (rozptyl {cv_vol:.2f})")

    # 3) schody – rovná zelená čára bez pullbacků (na 15s svíčkách)
    closes = [c for c in _bucket(active, 15) if c]
    if len(closes) >= 12:
        rets = [b / a - 1 for a, b in zip(closes, closes[1:]) if a]
        moves = [r for r in rets if abs(r) > 0.0005]
        green = sum(1 for r in moves if r > 0) / len(moves) if moves else 0
        peak, max_dd = closes[0], 0.0
        for p in closes:
            peak = max(peak, p)
            max_dd = max(max_dd, 1 - p / peak)
        rise = closes[-1] / closes[0] - 1 if closes[0] else 0
        r2 = _r2([math.log(p) for p in closes if p > 0])
        if rise > 0.15 and green >= 0.85 and max_dd < 0.06 and r2 > 0.93 and len(moves) >= 8:
            score += 55
            reasons.append(f"schody: {green * 100:.0f} % zelených, max pullback {max_dd * 100:.1f} %, "
                           f"rovnost {r2:.2f}")
        elif rise > 0.15 and green >= 0.8 and max_dd < 0.1 and r2 > 0.9 and len(moves) >= 8:
            score += 30
            reasons.append(f"skoro schody ({green * 100:.0f} % zelených, pullback max {max_dd * 100:.1f} %)")

        # 4) pila – pravidelné pump/pullback cykly
        sw = _zigzag(closes, 0.04)
        ups = [s for s in sw if s[0] == 1]
        downs = [s for s in sw if s[0] == -1]
        if len(ups) >= 3 and len(downs) >= 3:
            cv_up = _cv([s[1] for s in ups])
            cv_dn = _cv([s[1] for s in downs])
            cv_len = _cv([s[2] for s in sw])
            if cv_up is not None and cv_dn is not None and cv_len is not None \
                    and cv_up < 0.3 and cv_dn < 0.3 and cv_len < 0.35:
                score += 50
                reasons.append(f"pila: {len(sw)} pravidelných cyklů pump/pullback "
                               f"(~{st.fmean(s[1] for s in ups) * 100:.0f} % nahoru, "
                               f"~{st.fmean(s[1] for s in downs) * 100:.0f} % dolů)")

    # 5) wash trading – obří objem, cena stojí
    total_vol = sum(vols)
    span = active[-1][0] - active[0][0]
    if mcap and span >= 120:
        first, last = active[0][4], active[-1][4]
        drift = abs(last / first - 1) if first else 1
        rng = (max(c[2] for c in active) / min(c[3] for c in active) - 1) if min(c[3] for c in active) else 1
        if total_vol > 1.5 * mcap and drift < 0.08 and rng < 0.2:
            score += 50
            reasons.append(f"wash trading: objem ${total_vol:,.0f} za {span / 60:.0f} min "
                           f"při MC ${mcap:,.0f}, cena stojí")

    # metriky aktivity pro gem skóre (nezávislé na verdiktu)
    now_t = active[-1][0]
    last2 = sum(c[5] for c in active if c[0] >= now_t - 120)
    prev = sum(c[5] for c in active if now_t - 600 <= c[0] < now_t - 120) / 4 or 0
    res["m"] = {
        "active_sec": len(active),
        "vol_sum": sum(vols),
        "vol_trend": (last2 / prev) if prev else None,     # >1 = objem zrychluje
        "chg": (active[-1][4] / active[0][4] - 1) if active[0][4] else 0,
        "span_sec": active[-1][0] - active[0][0],
    }
    res["score"] = min(100, score)
    res["reasons"] = reasons
    res["verdict"] = "bot" if score >= SENS.get(sensitivity, 50) else "ok"
    return res
