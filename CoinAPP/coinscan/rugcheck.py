"""RugCheck API – bezpečnostní report tokenu (mint/freeze authority, holdeři, rizika).

Sdílený limit pro celou appku (scanner + Pulse): max 1 dotaz za `MIN_INTERVAL` s.
Po 429 (rate limit) se RugCheck na chvíli úplně pozastaví (cooldown), ať se limit neprohlubuje."""
import threading
import time

import requests

from . import status
from .config import env

BASE = "https://api.rugcheck.xyz/v1"
MIN_INTERVAL = 1.5          # s mezi dotazy (bez klíče je limit ~1/s)
_lock = threading.Lock()
_last_call = 0.0
_cooldown_until = 0.0
_cooldown_len = 30.0
last_status = ""            # poslední výsledek – pro log / GUI
calls_total = 0
_key_broken = False         # klíč vrátil 401 -> jedeme bez něj


class RateLimited(Exception):
    pass


def cooldown_left() -> float:
    return max(0.0, _cooldown_until - time.time())


def report(mint: str) -> dict | None:
    """Vrátí report, None když token RugCheck nezná / chyba.
    Vyhodí RateLimited, když je RugCheck v cooldownu – volající má coin zkusit později."""
    global _last_call, _cooldown_until, _cooldown_len, last_status, calls_total, _key_broken
    headers = {"Accept": "application/json"}
    key = "" if _key_broken else env("RUGCHECK_API_KEY")
    if key:
        headers["X-API-KEY"] = key
    interval = 0.5 if key else MIN_INTERVAL

    with _lock:  # jeden dotaz naráz v celé appce
        if cooldown_left() > 0:
            raise RateLimited(f"RugCheck cooldown {cooldown_left():.0f}s")
        wait = interval - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        calls_total += 1
        try:
            r = requests.get(f"{BASE}/tokens/{mint}/report", headers=headers, timeout=12)
        except requests.RequestException as e:
            last_status = f"chyba sítě: {e.__class__.__name__}"
            status.fail("RugCheck", last_status)
            return None
        if r.status_code == 429:
            _cooldown_until = time.time() + _cooldown_len
            last_status = f"rate limit – pauza {_cooldown_len:.0f}s"
            _cooldown_len = min(_cooldown_len * 2, 300)
            status.fail("RugCheck", last_status)
            raise RateLimited(last_status)
        _cooldown_len = 30.0
        if r.status_code in (401, 403) and key:
            # neplatný / neaktivní klíč -> veřejné API bez klíče funguje
            _key_broken = True
            status.fail("RugCheck", "API klíč odmítnut (401) – jedu bez klíče")
            try:
                r = requests.get(f"{BASE}/tokens/{mint}/report", headers={"Accept": "application/json"}, timeout=12)
            except requests.RequestException as e:
                last_status = f"chyba sítě: {e.__class__.__name__}"
                return None
        if r.status_code >= 400:
            last_status = f"HTTP {r.status_code}"
            return None
        last_status = "ok"
        status.ok("RugCheck", "", time.time() - _last_call)
        try:
            return _parse(r.json())
        except ValueError:
            last_status = "neplatná odpověď"
            return None


def _parse(d: dict) -> dict:
    holders = d.get("topHolders") or []
    # LP / AMM účty nepočítáme mezi holdery
    lp_owners = {m.get("pubkey") for m in d.get("markets") or []}
    real = [h for h in holders if h.get("owner") not in lp_owners and h.get("address") not in lp_owners]
    top10 = sum(float(h.get("pct") or 0) for h in real[:10])
    insiders = sum(1 for h in real if h.get("insider"))
    risks = [
        {"name": r.get("name"), "level": r.get("level"), "description": r.get("description")}
        for r in d.get("risks") or []
    ]
    score = d.get("score_normalised")
    if score is None:
        score = d.get("score")
    return {
        "score": score,
        "mint_authority": bool(d.get("mintAuthority")),
        "freeze_authority": bool(d.get("freezeAuthority")),
        "top10_pct": round(top10, 2),
        "insiders": insiders,
        "rugged": bool(d.get("rugged")),
        "risks": risks,
        "has_danger": any(r["level"] == "danger" for r in risks),
    }
