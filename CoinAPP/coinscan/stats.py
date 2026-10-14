"""Vyhodnocení úspěšnosti nastavení – hit rate podle fáze, AI verdiktu a risk ratingu."""
from . import db

GROUPS = {
    "Fáze filtru": "stage",
    "AI verdikt": "llm_verdict",
    "Risk rating": "risk_label",
}


def summary() -> list[dict]:
    out = []
    for title, col in GROUPS.items():
        rows = db.query(f"""
            SELECT COALESCE({col}, '—') AS grp,
                   COUNT(*) AS total,
                   SUM(outcome IN ('hit','miss','rug')) AS done,
                   SUM(outcome='hit') AS hits,
                   SUM(outcome='rug') AS rugs,
                   AVG(CASE WHEN outcome IN ('hit','miss','rug') THEN peak_mult END) AS avg_peak
            FROM coins WHERE outcome != 'untracked'
            GROUP BY grp ORDER BY total DESC""")
        for r in rows:
            done = r["done"] or 0
            out.append({
                "group": title, "value": r["grp"], "total": r["total"], "done": done,
                "hits": r["hits"] or 0, "rugs": r["rugs"] or 0,
                "hit_rate": (r["hits"] or 0) / done if done else None,
                "rug_rate": (r["rugs"] or 0) / done if done else None,
                "avg_peak": r["avg_peak"],
            })
    return out


STAGE_CZ = {
    "passed": "✅ Prošel vším",
    "rejected_basic": "Neprošel základním filtrem",
    "rejected_pattern": "🤖 Bot / fake objem",
    "rejected_safety": "🛡 Neprošel bezpečností",
    "rejected_llm": "AI řekla SKIP",
    "llm_error": "AI chyba",
}


def _cat(reason: str | None) -> str:
    if not reason:
        return "—"
    r = reason
    for sep in ("(", ":"):
        if sep in r and not r.startswith("AI"):
            r = r.split(sep)[0]
    if r.startswith("AI "):
        return "AI " + r.split()[1]
    return r.strip()


def dashboard(target: float = 2.0) -> dict:
    """Všechno pro záložku Úspěšnost v lidské řeči."""
    rows = [dict(r) for r in db.query(
        "SELECT stage, reject_reason, outcome, peak_mult, llm_verdict, risk_label FROM coins "
        "WHERE outcome != 'untracked'")]
    done = [r for r in rows if r["outcome"] in ("hit", "miss", "rug")]

    def grp(items):
        d = [r for r in items if r["outcome"] in ("hit", "miss", "rug")]
        hits = sum(1 for r in d if (r["peak_mult"] or 0) >= target)
        rugs = sum(1 for r in d if r["outcome"] == "rug")
        peaks = [r["peak_mult"] or 1 for r in d]
        return {"total": len(items), "done": len(d), "hits": hits, "rugs": rugs,
                "hit_rate": hits / len(d) if d else None, "rug_rate": rugs / len(d) if d else None,
                "avg_peak": sum(peaks) / len(peaks) if peaks else None,
                "x5": sum(1 for p in peaks if p >= 5), "x10": sum(1 for p in peaks if p >= 10)}

    passed = [r for r in rows if r["stage"] == "passed"]
    rejected = [r for r in rows if r["stage"] != "passed"]
    by_stage = {s: grp([r for r in rows if r["stage"] == s]) for s in STAGE_CZ if any(r["stage"] == s for r in rows)}
    reasons: dict[str, list] = {}
    for r in rejected:
        reasons.setdefault(_cat(r["reject_reason"]), []).append(r)
    by_reason = sorted(((k, grp(v)) for k, v in reasons.items()), key=lambda x: -x[1]["total"])
    by_verdict = {v: grp([r for r in rows if r["llm_verdict"] == v]) for v in ("BUY", "WATCH", "SKIP")
                  if any(r["llm_verdict"] == v for r in rows)}
    return {"all": grp(rows), "passed": grp(passed), "rejected": grp(rejected), "by_stage": by_stage,
            "by_reason": by_reason, "by_verdict": by_verdict, "target": target, "n_done": len(done)}
