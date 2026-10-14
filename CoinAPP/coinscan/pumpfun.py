"""pump.fun frontend API (neoficiální, zdarma, bez klíče).

Bonding curve: 793.1M tokenů je na prodej v křivce. Když se vyprodají, coin "migruje"
(complete=true) na PumpSwap. Progress = kolik z těch 793.1M už je prodáno.
"""
import time

import requests

BASE = "https://frontend-api-v3.pump.fun"
CURVE_TOKENS_RAW = 793_100_000 * 10**6  # 6 decimals
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
}
_session = requests.Session()
_session.headers.update(HEADERS)


def _get(path: str, params: dict | None = None):
    r = _session.get(BASE + path, params=params, timeout=8)
    r.raise_for_status()  # 429 -> výjimka, Pulse si dá pauzu
    return r.json()


def list_coins(sort: str = "created_timestamp", limit: int = 50, offset: int = 0, **extra) -> list[dict]:
    params = {"offset": offset, "limit": limit, "sort": sort, "order": "DESC", "includeNsfw": "false", **extra}
    data = _get("/coins", params) or []
    if isinstance(data, dict):
        data = data.get("coins") or data.get("data") or []
    return [normalize(c) for c in data if c.get("mint")]


def coin(mint: str) -> dict | None:
    data = _get(f"/coins/{mint}")
    return normalize(data) if data and data.get("mint") else None


def progress(c: dict) -> float:
    if c.get("complete"):
        return 100.0
    rtr = c.get("real_token_reserves")
    if rtr is None:
        return 0.0
    return max(0.0, min(100.0, (1 - float(rtr) / CURVE_TOKENS_RAW) * 100))


def normalize(c: dict) -> dict:
    now_ms = time.time() * 1000
    created = c.get("created_timestamp") or now_ms
    mcap = c.get("usd_market_cap") or c.get("market_cap_usd") or 0
    return {
        "mint": c["mint"],
        "name": c.get("name") or "",
        "symbol": c.get("symbol") or "",
        "image": c.get("image_uri") or "",
        "description": c.get("description") or "",
        "creator": c.get("creator") or "",
        "created_ms": created,
        "age_min": (now_ms - created) / 60000,
        "complete": bool(c.get("complete")),
        "progress": progress(c),
        "mcap": float(mcap or 0),
        "_mcap_sol": float(c.get("market_cap") or 0),
        "ath_mcap": float(c.get("ath_market_cap") or mcap or 0),
        "ath_ms": c.get("ath_market_cap_timestamp"),
        "sol_in_curve": (c.get("real_sol_reserves") or 0) / 1e9,
        "replies": c.get("reply_count") or 0,
        "last_trade_ms": c.get("last_trade_timestamp"),
        "twitter": c.get("twitter") or "",
        "telegram": c.get("telegram") or "",
        "website": c.get("website") or "",
        "is_live": bool(c.get("is_currently_live")),
        "pool": c.get("pump_swap_pool") or c.get("raydium_pool") or c.get("pool_address") or "",
    }


def has_socials(c: dict) -> bool:
    return bool(c.get("twitter") or c.get("telegram") or c.get("website"))
