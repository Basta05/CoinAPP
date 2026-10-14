"""Discord alerty – přes webhook, nebo přes tvého vlastního bota (REST API)."""
import requests

from .config import env

COLORS = {"LOW": 0x2ECC71, "MEDIUM": 0xF1C40F, "HIGH": 0xE74C3C}


def send(coin: dict) -> bool:
    embed = {
        "title": f"{coin.get('symbol')} – {coin.get('llm_verdict')} ({coin.get('risk_label')} risk {coin.get('risk_score')})",
        "url": coin.get("url"),
        "color": COLORS.get(coin.get("risk_label"), 0x95A5A6),
        "description": coin.get("llm_summary") or "",
        "fields": [
            {"name": "MCap", "value": _usd(coin.get("mcap0")), "inline": True},
            {"name": "Likvidita", "value": _usd(coin.get("liq0")), "inline": True},
            {"name": "Stáří", "value": f"{coin.get('age_min0') or 0:.0f} min", "inline": True},
            {"name": "AI confidence", "value": f"{(coin.get('llm_confidence') or 0) * 100:.0f} %", "inline": True},
            {"name": "Top10 holdeři", "value": f"{coin.get('top10_pct') or 0:.1f} %", "inline": True},
            {"name": "CA", "value": f"`{coin.get('address')}`", "inline": False},
        ],
    }
    body = {"embeds": [embed]}

    webhook = env("DISCORD_WEBHOOK_URL")
    token, channel = env("DISCORD_BOT_TOKEN"), env("DISCORD_CHANNEL_ID")
    if webhook:
        r = requests.post(webhook, json=body, timeout=15)
    elif token and channel:
        r = requests.post(f"https://discord.com/api/v10/channels/{channel}/messages",
                          headers={"Authorization": f"Bot {token}"}, json=body, timeout=15)
    else:
        raise RuntimeError("Discord není nastavený (.env: webhook nebo bot token + channel ID)")
    return r.status_code < 300


def _usd(v):
    if not v:
        return "?"
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}k"
