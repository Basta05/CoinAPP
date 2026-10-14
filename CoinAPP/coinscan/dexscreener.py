"""DexScreener API – zdarma, bez klíče. Limit ~60 req/min (profily) a ~300 req/min (tokens)."""
import time

import requests

from . import status

BASE = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "CoinScan/0.1", "Accept": "application/json"}


def _get(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                status.fail("DexScreener", "rate limit")
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            status.ok("DexScreener")
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                status.fail("DexScreener", str(e))
                raise
            time.sleep(2)
    return None


def latest_candidates(chain: str, sources: list[str]) -> dict[str, dict]:
    """Vrátí {token_address: profil} z vybraných zdrojů."""
    endpoints = {
        "token_profiles": "/token-profiles/latest/v1",
        "token_boosts": "/token-boosts/latest/v1",
    }
    out: dict[str, dict] = {}
    for src in sources:
        path = endpoints.get(src)
        if not path:
            continue
        data = _get(BASE + path) or []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            if item.get("chainId") != chain:
                continue
            addr = item.get("tokenAddress")
            if addr and addr not in out:
                item["_source"] = src
                out[addr] = item
    return out


def pairs_for_tokens(chain: str, addresses: list[str]) -> dict[str, dict]:
    """Vrátí {token_address: nejlikvidnější pár}. Max 30 adres na request."""
    result: dict[str, dict] = {}
    for i in range(0, len(addresses), 30):
        chunk = addresses[i:i + 30]
        data = _get(f"{BASE}/tokens/v1/{chain}/{','.join(chunk)}") or []
        if isinstance(data, dict):
            data = data.get("pairs") or []
        for pair in data:
            addr = (pair.get("baseToken") or {}).get("address")
            if addr not in chunk:
                continue
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            best = result.get(addr)
            if best is None or liq > ((best.get("liquidity") or {}).get("usd") or 0):
                result[addr] = pair
    return result


def pair_metrics(pair: dict) -> dict:
    """Vytáhne z páru čísla, se kterými pracujeme."""
    txns = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    info = pair.get("info") or {}
    created = pair.get("pairCreatedAt")
    age_min = (time.time() * 1000 - created) / 60000 if created else None
    h1 = txns.get("h1") or {}
    return {
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "name": (pair.get("baseToken") or {}).get("name"),
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
        "url": pair.get("url"),
        "price": _f(pair.get("priceUsd")),
        "mcap": _f(pair.get("marketCap") or pair.get("fdv")),
        "liquidity": _f((pair.get("liquidity") or {}).get("usd")),
        "volume_h1": _f(vol.get("h1")),
        "volume_h24": _f(vol.get("h24")),
        "buys_m5": (txns.get("m5") or {}).get("buys", 0),
        "sells_m5": (txns.get("m5") or {}).get("sells", 0),
        "buys_h1": h1.get("buys", 0),
        "sells_h1": h1.get("sells", 0),
        "price_change": pair.get("priceChange") or {},
        "pair_created_at": created,
        "age_min": age_min,
        "websites": [w.get("url") for w in info.get("websites") or [] if w.get("url")],
        "socials": [{"type": s.get("type"), "url": s.get("url")} for s in info.get("socials") or []],
    }


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
