"""LLM kontrola metadat coinu přes OpenRouter."""
import json
import re

import threading
import time

import requests

from . import status
from .config import env

URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = """Jsi zkušený trader Solana memecoinů (pump.fun). Hledáš coiny s potenciálem 10–20×,
ale hlavně chráníš kapitál. Dostaneš data o tokenu (metadata, trh, bonding curve, RugCheck, bot filtr,
gem skóre a doporučenou velikost pozice podle risk managementu uživatele).

Posuď:
1) BEZPEČNOST – rug/scam znaky: mint/freeze authority, koncentrace holderů, insideři, bundlované nákupy,
   fake objem, kopie známých projektů/impersonace, mrtvé nebo podezřelé linky, generický AI popis.
2) POTENCIÁL – síla narativu/meme, komunita (socials, komentáře, live), momentum (rychlost bonding
   curve, růst MC, vzdálenost od ATH), prostor k růstu vzhledem k MC.
3) STRATEGII – pokud dává smysl vstoupit: kde vstoupit (teď / počkat na pullback k MC X),
   stop-loss, take-profity, jak dlouho držet. Buď konkrétní v MC ($).

Pravidla: BUY jen když je bezpečnost OK a momentum reálné. Při pochybnostech WATCH nebo SKIP.
Piš česky, stručně, jako zkušený kamarád-trader.

Odpověz POUZE validním JSON:
{"verdict": "BUY" | "WATCH" | "SKIP",
 "confidence": 0.0-1.0,
 "safety": "OK" | "RISKY" | "DANGER",
 "potential": "LOW" | "MID" | "HIGH",
 "narrative": "o čem coin je (1 věta)",
 "thesis": "proč by mohl letět (1-2 věty)",
 "red_flags": ["..."],
 "strategy": {"entry": "kdy/kde vstoupit", "entry_max_mcap": číslo nebo null,
              "stop_loss_mcap": číslo nebo null, "take_profits": ["2× prodat 50 %", "..."],
              "hold": "jak dlouho", "invalidation": "kdy to vzdát"},
 "summary": "1-2 věty shrnutí"}"""


# ŽÁDNÉ výchozí modely v kódu ani v config.yaml – modely jdou VÝHRADNĚ z .env:
#   OPENROUTER_FREE_MODELS  (jen :free modely nebo openrouter/free)
#   OPENROUTER_PAID_MODELS
#   JEV_MODEL
# Chybí model nebo klíč -> ta úroveň AI se vůbec nepoužije.


class BudgetExceeded(Exception):
    pass


def is_free(model: str) -> bool:
    return model.endswith(":free") or model == "openrouter/free"


def api_key(free: bool) -> str:
    """Oddělené klíče: free = OPENROUTER_API_KEY_FREE, placený = OPENROUTER_API_KEY_PAID. Žádná záloha."""
    k = env("OPENROUTER_API_KEY_FREE" if free else "OPENROUTER_API_KEY_PAID")
    if not k:
        raise RuntimeError("Chybí " + ("OPENROUTER_API_KEY_FREE" if free else "OPENROUTER_API_KEY_PAID") + " v .env")
    return k


def _env_list(name: str) -> list[str]:
    return [m.strip() for m in env(name).split(",") if m.strip()]


def free_models() -> list[str]:
    """Free modely jen z .env. Placený model sem nepropadne (bere jen :free / openrouter/free)."""
    return [m for m in _env_list("OPENROUTER_FREE_MODELS") if is_free(m)][:3]


def paid_models() -> list[str]:
    return _env_list("OPENROUTER_PAID_MODELS")[:3]


def jev_model() -> str:
    return env("JEV_MODEL")


def has_jev() -> bool:
    return bool(env("JEV_API_KEY") and jev_model())


def tiers_info() -> str:
    f, p, j = free_models(), paid_models(), jev_model()
    return (f"free: {', '.join(f) if f and env('OPENROUTER_API_KEY_FREE') else 'VYPNUTO'} | "
            f"placený: {', '.join(p) if p and env('OPENROUTER_API_KEY_PAID') else 'VYPNUTO'} | "
            f"Jev: {j if has_jev() else 'VYPNUTO'}")


# ------------------------------------------------------------------ Jev (TypeSafe System One)
JEV_QUESTIONS = {
    "scam": {"type": "noul",
             "instructions": "Je tenhle Solana memecoin pravděpodobně scam, rug, fake objem (boti) nebo kopie/impersonace?",
             "criteria": {"true": "scam / rug / fake objem / impersonace", "false": "vypadá jako legitimní coin s reálnými lidmi"}},
    "potential": {"type": "score",
                  "instructions": "Jaký má coin potenciál na velký růst (5–20×) v příštích hodinách?",
                  "criteria": ["mrtvý / slop", "průměrný, spíš nic", "slušné momentum", "silný kandidát na 5–20×"]},
    "action": {"type": "choice",
               "instructions": "Co by měl zkušený memecoin trader udělat?",
               "criteria": {"BUY": "vstoupit teď", "WATCH": "sledovat, zatím nevstupovat", "SKIP": "ignorovat"}},
}


def jev_score(scam: float, pot: float, act: str) -> int:
    """Jev skóre 0–100: potenciál × (1 − scam), plus/minus podle doporučené akce."""
    v = (pot / 3) * (1 - scam) + {"BUY": 0.1, "WATCH": 0.0, "SKIP": -0.15}.get(act, 0)
    return int(round(max(0.0, min(1.0, v)) * 100))


def jev_screen(profile: dict, metrics: dict, rc: dict | None, cfg: dict | None = None) -> dict:
    """Jev: typovaná rozhodnutí s pravděpodobnostmi, ~0.1–0.5 s, ~$0.00003 za coin."""
    lc = _llm_cfg(cfg)
    key = env("JEV_API_KEY")
    if not key:
        raise RuntimeError("Chybí JEV_API_KEY v .env")
    if not jev_model():
        raise RuntimeError("Chybí JEV_MODEL v .env – Jev vypnutý")
    # klíč z OpenRouteru (sk-or-...) jde přes OpenRouter, jinak přímo na TypeSafe
    url = ("https://openrouter.ai/api/v1/systemone" if key.startswith("sk-or-")
           else "https://api.typesafe.ai/v1/systemone")
    t0 = time.time()
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": jev_model(), "state": json.loads(_payload(profile, metrics, rc)),
                                "questions": JEV_QUESTIONS}, timeout=15)
    except requests.RequestException as e:
        status.fail("AI", f"Jev: {e}")
        raise RuntimeError(f"Jev nedostupný: {e}")
    if r.status_code >= 400:
        status.fail("AI", f"Jev HTTP {r.status_code}")
        raise RuntimeError(f"Jev {r.status_code}: {r.text[:150]}")
    d = r.json()
    a = d.get("answers") or {}
    scam = float((a.get("scam") or {}).get("noul") or 0)
    pot = float((a.get("potential") or {}).get("score") or 0)        # 0–3
    act = (a.get("action") or {}).get("choice") or "SKIP"
    act_p = ((a.get("action") or {}).get("probabilities") or {}).get(act)
    usage = d.get("usage") or {}
    cost = float(usage.get("cost") or (usage.get("input_tokens") or 0) * 0.042 / 1e6)
    if cost:
        _add_spend(cost)
    status.ok("AI", "jev", time.time() - t0)
    passed = scam <= lc.get("jev_max_scam", 0.4) and pot >= lc.get("jev_min_potential", 1.4) and act != "SKIP"
    return {"pass": passed, "score": jev_score(scam, pot, act),
            "safety": "OK" if scam < 0.2 else "RISKY" if scam < 0.5 else "DANGER",
            "potential": "HIGH" if pot >= 2.2 else "MID" if pot >= 1.2 else "LOW",
            "hint": act,
            "reason": f"scam {scam * 100:.0f} %, potenciál {pot:.1f}/3, doporučení {act}"
                      + (f" ({act_p * 100:.0f} %)" if act_p else ""),
            "model": d.get("model", "jev"), "cost": cost, "jev": {"scam": scam, "potential": pot, "action": act}}


# ------------------------------------------------------------------ rozpočet ($ / den)
_budget_lock = threading.Lock()
_paid_broken_until = 0.0     # 402 = došel kredit -> placené chvíli nezkoušet


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def spent_today() -> tuple[float, int]:
    from . import db
    try:
        rows = db.query("SELECT usd, calls FROM ai_usage WHERE day=?", (_today(),))
    except Exception:  # noqa: BLE001
        return 0.0, 0
    return (rows[0]["usd"], rows[0]["calls"]) if rows else (0.0, 0)


def _add_spend(usd: float):
    from . import db
    with _budget_lock:
        with db.connect() as con:
            con.execute("INSERT INTO ai_usage(day, usd, calls) VALUES (?, ?, 1) "
                        "ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd, calls = calls + 1",
                        (_today(), usd))


def _llm_cfg(cfg: dict | None) -> dict:
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    out = dict(cfg.get("llm") or {})
    # modely NIKDY z config.yaml – jen z .env
    for k in ("free_models", "report_models", "screen_models", "jev_model"):
        out.pop(k, None)
    return out


def paid_available(cfg: dict | None = None) -> tuple[bool, str]:
    lc = _llm_cfg(cfg)
    if not paid_models():
        return False, "v .env není OPENROUTER_PAID_MODELS"
    if not env("OPENROUTER_API_KEY_PAID"):
        return False, "v .env není OPENROUTER_API_KEY_PAID"
    if not lc.get("use_paid", True):
        return False, "placené AI vypnuté"
    if time.time() < _paid_broken_until:
        return False, "došel kredit na OpenRouteru"
    usd, _ = spent_today()
    if usd >= lc.get("max_usd_per_day", 2.0):
        return False, f"denní limit ${lc.get('max_usd_per_day', 2.0):.2f} vyčerpán"
    return True, ""


# ------------------------------------------------------------------ volání
def _call(messages: list[dict], temperature: float, max_tokens: int = 1600, models: list[str] | None = None,
          web: bool = False, reasoning: dict | None = None, timeout: int = 90) -> tuple[str, str, float]:
    """Vrátí (text, použitý model, cena v $)."""
    global _paid_broken_until
    ms = [m for m in (models or []) if m][:3]
    if not ms:
        raise RuntimeError("žádný model v .env – AI vypnutá")
    key = api_key(free=is_free(ms[0]))
    body = {
        "model": ms[0], "models": ms, "temperature": temperature, "max_tokens": max_tokens,
        "reasoning": reasoning or {"effort": "low", "exclude": True},
        "messages": messages, "usage": {"include": True},
    }
    if web:
        body["plugins"] = [{"id": "web", "max_results": 5,
                            "search_prompt": "Aktuální informace o tomto Solana memecoinu (X/Twitter, scam, rug):"}]
    t0 = time.time()
    try:
        r = requests.post(URL, headers={"Authorization": f"Bearer {key}", "HTTP-Referer": "https://localhost/coinscan",
                                        "X-Title": "CoinScan"}, json=body, timeout=timeout)
    except requests.Timeout:
        status.fail("AI", f"timeout {timeout} s")
        raise RuntimeError(f"AI neodpověděla do {timeout} s")
    except requests.RequestException as e:
        status.fail("AI", str(e))
        raise RuntimeError(f"AI nedostupná: {e}")
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if r.status_code >= 400 or err:
        msg = (err or {}).get("message") or r.text[:200]
        raw = ((err or {}).get("metadata") or {}).get("raw")
        code = (err or {}).get("code") or r.status_code
        if str(code) == "402":
            _paid_broken_until = time.time() + 900
            msg = "došel kredit na OpenRouteru (dobij na openrouter.ai/credits) – " + str(msg)
        text = f"{code}: {msg}" + (f" ({str(raw)[:120]})" if raw else "")
        status.fail("AI", text)
        raise RuntimeError(text)
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning") or ""
    if not content.strip():
        status.fail("AI", "prázdná odpověď")
        raise RuntimeError("AI vrátila prázdnou odpověď")
    used = data.get("model", ms[0])
    cost = float((data.get("usage") or {}).get("cost") or 0)
    if cost:
        _add_spend(cost)
    status.ok("AI", used.split("/")[-1], time.time() - t0)
    return content, used, cost


def _payload(profile: dict, metrics: dict, rc: dict | None) -> str:
    market_keys = [k for k in metrics if k not in ("symbol", "name", "websites", "socials")]
    return json.dumps({
        "token": {
            "symbol": metrics.get("symbol"), "name": metrics.get("name"),
            "description": (profile.get("description") or "")[:1500],
            "links": [l.get("url") for l in profile.get("links") or []] + metrics.get("websites", []),
            "socials": metrics.get("socials"),
        },
        "market": {k: metrics.get(k) for k in market_keys},
        "rugcheck": rc,
    }, ensure_ascii=False, default=str)


UPDATE_SYSTEM = """Jsi zkušený memecoin trader a hlídáš pozici v BUY gemu, který jsi dřív doporučil.
Dostaneš původní report (vstupní MC, strategie), vývoj MC od alertu po cyklech (1 cyklus = pár desítek sekund),
vývoj Jev skóre (0–100, TypeSafe Jev: scam × potenciál) a aktuální data.
Řekni stručně, co teď: držet, přikoupit, vybrat zisk, nebo prodat. Buď konkrétní v MC ($) a respektuj původní strategii
(SL/TP). Vyplatí se ještě vstoupit pro někoho, kdo nevstoupil?
Odpověz POUZE JSON:
{"action": "HOLD" | "ADD" | "TAKE_PROFIT" | "SELL",
 "trend": "UP" | "SIDEWAYS" | "DOWN",
 "confidence": 0.0-1.0,
 "worth_entering_now": true | false,
 "summary": "1-2 věty česky – co se děje a co dělat",
 "levels": "důležité MC úrovně (support / TP / SL), 1 věta"}"""


def ai_update(payload: dict, cfg: dict | None = None, allow_paid: bool = False) -> dict:
    """AI update BUY gemu: free (hlavní), placený jen když povolíš v nastavení a free nejde."""
    global _free_cooldown_until
    lc = _llm_cfg(cfg)
    msgs = [{"role": "system", "content": UPDATE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
    errors = []
    tiers = []
    ok, why = free_available(cfg)
    if ok:
        tiers.append((free_models(), False))
    else:
        errors.append(f"free: {why}")
    if allow_paid and paid_available(cfg)[0]:
        tiers.append((paid_models(), True))
    for models, paid in tiers:
        if not paid:
            _free_times.append(time.time())
        try:
            text, used, cost = _call(msgs, 0.2, 600, models, timeout=lc.get("free_timeout", 60) if not paid else 60)
            t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).replace("```json", "").replace("```", "")
            m = re.search(r"\{.*\}", t, re.S)
            d = json.loads(m.group(0) if m else t)
            act = str(d.get("action", "HOLD")).upper()
            return {"action": act if act in ("HOLD", "ADD", "TAKE_PROFIT", "SELL") else "HOLD",
                    "trend": str(d.get("trend", "?")).upper(), "confidence": float(d.get("confidence") or 0),
                    "worth": bool(d.get("worth_entering_now")), "summary": str(d.get("summary", "")),
                    "levels": str(d.get("levels", "")), "model": used, "cost": cost, "paid": paid}
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) and not paid:
                _free_cooldown_until = time.time() + 300
            errors.append(("placený: " if paid else "free: ") + str(e)[:120])
    raise RuntimeError(" | ".join(errors) or "AI update nejde")


_free_times: list[float] = []
_free_cooldown_until = 0.0


def free_available(cfg: dict | None = None) -> tuple[bool, str]:
    lc = _llm_cfg(cfg)
    if not free_models():
        return False, "v .env není OPENROUTER_FREE_MODELS (jen :free modely)"
    if not env("OPENROUTER_API_KEY_FREE"):
        return False, "v .env není OPENROUTER_API_KEY_FREE"
    now = time.time()
    _free_times[:] = [t for t in _free_times if now - t < 3600]
    if now < _free_cooldown_until:
        return False, f"free pauza {_free_cooldown_until - now:.0f}s (rate limit)"
    if len(_free_times) >= lc.get("max_free_per_hour", 15):
        return False, f"free limit {lc.get('max_free_per_hour', 15)}/hod"
    return True, ""


def report(profile: dict, metrics: dict, rc: dict | None, cfg: dict | None = None, allow_paid: bool = True) -> dict:
    """Plný AI report. HLAVNÍ = free model. Placený jen jako pojistka, když free nejde (limit/chyba/timeout)."""
    global _free_cooldown_until
    lc = _llm_cfg(cfg)
    errors = []
    ok, why = free_available(cfg)
    if ok:
        _free_times.append(time.time())
        try:
            text, used, cost = _call([{"role": "system", "content": SYSTEM},
                                      {"role": "user", "content": _payload(profile, metrics, rc)}],
                                     lc.get("temperature", 0.2), 1800, free_models(), timeout=lc.get("free_timeout", 60))
            out = parse(text)
            out.update(model=used, cost=cost, paid=False)
            return out
        except Exception as e:  # noqa: BLE001
            if "429" in str(e):
                _free_cooldown_until = time.time() + 300
            errors.append(f"free: {str(e)[:120]}")
    else:
        errors.append(f"free: {why}")
    paid_ok, why_p = paid_available(cfg)
    if allow_paid and paid_ok:
        try:
            text, used, cost = _call([{"role": "system", "content": SYSTEM},
                                      {"role": "user", "content": _payload(profile, metrics, rc)}],
                                     lc.get("temperature", 0.2), 1800, paid_models(),
                                     web=lc.get("report_web_search", True), timeout=90)
            out = parse(text)
            out.update(model=used, cost=cost, paid=True, free_error=errors[-1] if errors else "")
            return out
        except Exception as e:  # noqa: BLE001
            errors.append(f"placený: {str(e)[:120]}")
    elif allow_paid:
        errors.append(f"placený: {why_p}")
    raise RuntimeError(" | ".join(errors))


def pipeline(profile: dict, metrics: dict, rc: dict | None, cfg: dict | None = None) -> tuple[dict | None, dict | None]:
    """Scanner: Jev (když je klíč) -> report (free, placený jen jako pojistka). Vrátí (report, jev).
    report je None, když Jev coin jasně zamítl."""
    lc = _llm_cfg(cfg)
    jev = None
    if has_jev():
        try:
            jev = jev_screen(profile, metrics, rc, cfg)
            jev["tier"] = "jev"
            if lc.get("require_jev_pass", True) and not jev["pass"]:
                return None, jev
        except Exception:  # noqa: BLE001
            jev = None
    return report(profile, metrics, rc, cfg), jev


def test(cfg: dict | None = None) -> str:
    """Test z Nastavení – vyzkouší jen to, co je v .env (free, Jev, placený)."""
    lc = _llm_cfg(cfg)
    lines = ["Z .env: " + tiers_info()]
    for name, ms, keyname in (("free (hlavní)", free_models(), "OPENROUTER_API_KEY_FREE"),
                              ("placený (pojistka)", paid_models(), "OPENROUTER_API_KEY_PAID")):
        if not ms or not env(keyname):
            lines.append(f"– {name}: vypnuto (chybí {'model' if not ms else keyname} v .env)")
            continue
        t0 = time.time()
        try:
            text, used, cost = _call([{"role": "user", "content": 'Odpověz jen JSON: {"ok": true}'}], 0, 30, ms,
                                     timeout=60)
            lines.append(f"✓ {name}: {used} ({time.time() - t0:.1f} s, ${cost:.5f})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"✗ {name}: {str(e)[:120]}")
    if has_jev():
        t0 = time.time()
        try:
            j = jev_screen({"description": "test"}, {"symbol": "TEST", "name": "Test coin", "mcap_usd": 30000}, None, cfg)
            lines.insert(2, f"✓ Jev: {j['model']} ({time.time() - t0:.2f} s) – Jev skóre {j['score']}, {j['reason']}")
        except Exception as e:  # noqa: BLE001
            lines.insert(2, f"✗ Jev: {str(e)[:120]}")
    else:
        lines.insert(2, "– Jev: vypnuto (chybí JEV_API_KEY nebo JEV_MODEL v .env)")
    usd, calls = spent_today()
    lines.append(f"Dnes utraceno ${usd:.3f} za {calls} dotazů (limit placeného ${lc.get('max_usd_per_day', 2.0):.2f})")
    return "\n".join(lines)


def parse(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).replace("```json", "").replace("```", "")
    data = None
    # najít JSON objekt s "verdict" (odzadu – kdyby model něco psal před ním)
    for m in reversed(list(re.finditer(r"\{", text))):
        chunk = text[m.start():text.rfind("}") + 1]
        try:
            data = json.loads(chunk)
            if isinstance(data, dict) and "verdict" in data:
                break
        except ValueError:
            continue
    if not isinstance(data, dict):
        raise RuntimeError(f"AI nevrátila platný JSON: {text[:150]}")
    verdict = str(data.get("verdict", "SKIP")).upper()
    if verdict not in ("BUY", "WATCH", "SKIP"):
        verdict = "SKIP"
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    strat = data.get("strategy") if isinstance(data.get("strategy"), dict) else {}
    flags = data.get("red_flags") or []
    if isinstance(flags, str):
        flags = [flags]
    return {
        "verdict": verdict,
        "confidence": conf,
        "safety": str(data.get("safety", "")).upper() or "?",
        "potential": str(data.get("potential", "")).upper() or "?",
        "narrative": data.get("narrative", ""),
        "thesis": data.get("thesis", ""),
        "red_flags": [str(f) for f in flags],
        "strategy": strat,
        "summary": data.get("summary", ""),
    }
