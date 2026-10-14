"""Export logu / reportu gemů – CSV a Markdown. Bez AI, nebo s krátkým shrnutím od FREE AI."""
import csv
import json
import time
from datetime import datetime

from . import db, gems, llm

GEM_COLS = ["id", "ts", "symbol", "name", "address", "source", "stage", "gem_score", "jev0", "jev_score", "jev_action",
            "verdict", "confidence", "safety", "potential", "mcap0", "peak_mcap", "peak_mult", "last_mcap", "last_mult",
            "outcome", "stop_reason", "archived", "cycles", "ai_updates", "jev_updates", "last_action",
            "last_update_text", "summary", "thesis", "red_flags", "strategy", "model", "paid", "cost", "gem_reasons"]


def _ts(v):
    return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S") if v else ""


def gems_csv(path: str, since_h: float | None = None) -> int:
    q = "SELECT * FROM gems" + (" WHERE ts > ?" if since_h else "") + " ORDER BY ts"
    rows = [dict(r) for r in db.query(q, (time.time() - since_h * 3600,) if since_h else ())]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:      # utf-8-sig = Excel ukáže češtinu
        w = csv.writer(f, delimiter=";")
        w.writerow(GEM_COLS)
        for r in rows:
            w.writerow([_ts(r.get(c)) if c == "ts" else r.get(c) for c in GEM_COLS])
    return len(rows)


def _pct(a, b):
    return f"{a / b * 100:.0f} %" if b else "–"


def build_report(since_h: float = 24) -> str:
    since = time.time() - since_h * 3600
    rows = [dict(r) for r in db.query("SELECT * FROM gems WHERE ts > ? ORDER BY ts", (since,))]
    buy = [r for r in rows if r["verdict"] == "BUY"]
    other = [r for r in rows if r["verdict"] != "BUY"]
    L = [f"# CoinScan report – posledních {since_h:g} h", f"_vygenerováno {datetime.now():%d.%m.%Y %H:%M}_", ""]

    def tiers(rs):
        return " · ".join(f"{t}×+: {sum(1 for r in rs if (r['peak_mult'] or 0) >= t)}" for t in (2, 5, 10, 20))

    L += ["## Souhrn",
          f"- Gemů celkem: **{len(rows)}** (BUY {len(buy)}, WATCH {sum(1 for r in rows if r['verdict'] == 'WATCH')}, "
          f"jen Jev {sum(1 for r in rows if r['stage'] == 'jev' and not r['verdict'])}, "
          f"SKIP {sum(1 for r in rows if r['verdict'] == 'SKIP')})",
          f"- BUY od alertu: {tiers(buy)}",
          f"- Ostatní od alertu: {tiers(other)}",
          f"- Rugy mezi BUY: {sum(1 for r in buy if r['outcome'] == 'rug')} ({_pct(sum(1 for r in buy if r['outcome'] == 'rug'), len(buy))})",
          f"- Útrata AI: ${sum(r['cost'] or 0 for r in rows):.3f}", ""]

    # Jev jako prediktor
    buckets = {"Jev 70+": [], "Jev 50–69": [], "Jev pod 50": [], "bez Jevu": []}
    for r in rows:
        j = r.get("jev0") if r.get("jev0") is not None else r.get("jev_score")
        k = "bez Jevu" if j is None else "Jev 70+" if j >= 70 else "Jev 50–69" if j >= 50 else "Jev pod 50"
        buckets[k].append(r)
    L += ["## Trefuje se Jev skóre?", "| Skupina | Gemů | 2×+ | 5×+ | Rug |", "|---|---|---|---|---|"]
    for k, rs in buckets.items():
        if rs:
            L.append(f"| {k} | {len(rs)} | {_pct(sum(1 for r in rs if (r['peak_mult'] or 0) >= 2), len(rs))} | "
                     f"{_pct(sum(1 for r in rs if (r['peak_mult'] or 0) >= 5), len(rs))} | "
                     f"{_pct(sum(1 for r in rs if r['outcome'] == 'rug'), len(rs))} |")
    L.append("")

    L += ["## BUY gemy – detail"]
    for r in buy:
        L.append(f"### {r['symbol']} – max {(r['peak_mult'] or 1):.2f}× (teď {(r['last_mult'] or 1):.2f}×) · {r['outcome']}")
        L.append(f"- alert {_ts(r['ts'])} při MC ${r['mcap0'] or 0:,.0f} · gem {r['gem_score']} · Jev {r.get('jev0')} → "
                 f"{r.get('jev_score')} · AI {r['verdict']} {(r['confidence'] or 0) * 100:.0f} % ({(r['model'] or '').split('/')[-1]})")
        if r.get("summary"):
            L.append(f"- report: {r['summary']}")
        for u in gems.get_updates(r["id"], ("ai",)):
            d = json.loads(u["data"] or "{}")
            L.append(f"  - 🔄 cyklus {u['cycle']} ({(u['mult'] or 1):.2f}×): **{u['action']}** · {d.get('trend')} – {d.get('summary')}")
        L.append(f"- CA: `{r['address']}`")
        L.append("")

    L += ["## Ostatní gemy (archiv)", "| Čas | Coin | Gem | Jev | AI | MC při alertu | Max × | Výsledek |",
          "|---|---|---|---|---|---|---|---|"]
    for r in other:
        L.append(f"| {_ts(r['ts'])[11:16]} | {r['symbol']} | {r['gem_score']} | {r.get('jev_score') if r.get('jev_score') is not None else '–'} | "
                 f"{r['verdict'] or '–'} | ${r['mcap0'] or 0:,.0f} | {(r['peak_mult'] or 1):.2f}× | {r['outcome']} |")
    L.append("")

    run = [dict(r) for r in db.query("SELECT status, COUNT(*) n FROM runners WHERE detected_ts > ? GROUP BY status", (since,))]
    if run:
        tot = sum(r["n"] for r in run)
        caught = sum(r["n"] for r in run if r["status"].startswith("caught"))
        L += ["## 🏁 Runneři", f"- {tot} runnerů, chyceno {caught} ({_pct(caught, tot)})"]
        L += [f"  - {r['status']}: {r['n']}" for r in run]
        L.append("")
    return "\n".join(L)


def report_md(path: str, since_h: float = 24, with_ai: bool = False, cfg: dict | None = None) -> str:
    text = build_report(since_h)
    note = ""
    if with_ai:
        try:
            msgs = [{"role": "system", "content": "Jsi analytik memecoin detektoru. Z reportu napiš česky krátké shrnutí "
                                                  "(max 8 odrážek): co fungovalo, co ne, jestli Jev a AI BUY trefují, "
                                                  "a 2–3 konkrétní tipy na nastavení. Piš prostý Markdown."},
                    {"role": "user", "content": text[:24000]}]
            ok, why = llm.free_available(cfg)
            if not ok:
                raise RuntimeError(why)
            out, used, _ = llm._call(msgs, 0.3, 900, llm.free_models(), timeout=90)   # jen FREE AI z .env
            text = text.replace("## Souhrn", f"## 🤖 AI shrnutí ({used.split('/')[-1]})\n{out.strip()}\n\n## Souhrn", 1)
        except Exception as e:  # noqa: BLE001
            note = f"free AI shrnutí nešlo ({str(e)[:100]}) – uloženo bez něj"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return note
