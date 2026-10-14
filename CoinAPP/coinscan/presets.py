"""Presety filtrů. Built-in presety + vlastní uložené v presets/*.yaml.
Preset je "částečný config" – přepíše jen sekce/hodnoty, které obsahuje."""
import yaml

from .config import PRESETS_DIR, deep_merge

BUILTIN: dict[str, dict] = {
    # Hlavní styl: coiny kolem migrace, kdy už mají trakci, ale ještě jsou malé.
    "Migration play": {
        "_popis": "Coiny těsně před/po migraci z pump.fun. Vyvážený poměr šance a rizika – dobrý start.",
        "scanner": {"sources": ["token_profiles", "token_boosts", "pumpfun"], "pumpfun_min_progress": 70},
        "basic_filters": {"max_age_minutes": 180, "min_liquidity_usd": 5000, "max_liquidity_usd": 500000,
                          "min_mcap_usd": 0, "max_mcap_usd": 1500000, "min_volume_h1_usd": 5000,
                          "min_buys_m5": 10, "min_buy_sell_ratio_h1": 1.0, "require_socials": True},
        "safety_filters": {"max_rugcheck_score": 50, "max_top10_holders_pct": 30,
                           "reject_mint_authority": True, "reject_freeze_authority": True,
                           "reject_danger_risks": True},
        "llm": {"min_confidence": 0.55},
        "tracking": {"target_multiple": 2.0},
        "pulse": {"soon_progress": 60, "columns": {
            "new": {"min_mcap": 5000, "max_age_min": 30, "require_socials": False, "max_risk": "HIGH"},
            "soon": {"min_progress": 60, "max_age_min": 240, "require_socials": True, "max_risk": "HIGH"},
            "migrated": {"min_mcap": 30000, "max_age_min": 720, "require_socials": True, "max_risk": "MEDIUM"}}},
    },
    "Konzervativní": {
        "_popis": "Méně signálů, víc bezpečnosti. Starší coiny s ověřenou likviditou a rozloženými holdery.",
        "scanner": {"sources": ["token_profiles", "token_boosts", "pumpfun"], "pumpfun_min_progress": 100},
        "basic_filters": {"max_age_minutes": 720, "min_liquidity_usd": 30000, "max_liquidity_usd": 2000000,
                          "min_mcap_usd": 0, "max_mcap_usd": 5000000, "min_volume_h1_usd": 50000,
                          "min_buys_m5": 15, "min_buy_sell_ratio_h1": 1.0, "require_socials": True},
        "safety_filters": {"max_rugcheck_score": 40, "max_top10_holders_pct": 25,
                           "reject_mint_authority": True, "reject_freeze_authority": True,
                           "reject_danger_risks": True},
        "llm": {"min_confidence": 0.65},
        "tracking": {"target_multiple": 2.0},
        "pulse": {"soon_progress": 75, "columns": {
            "new": {"min_mcap": 10000, "max_age_min": 20, "require_socials": True, "max_risk": "MEDIUM"},
            "soon": {"min_progress": 75, "max_age_min": 180, "require_socials": True, "max_risk": "MEDIUM"},
            "migrated": {"min_mcap": 60000, "max_age_min": 720, "require_socials": True, "max_risk": "LOW"}}},
    },
    "Degen early": {
        "_popis": "Úplně čerstvé coiny. Nejvyšší potenciál i riziko – malé pozice, cíl 3×.",
        "scanner": {"sources": ["token_profiles", "pumpfun"], "pumpfun_min_progress": 30},
        "basic_filters": {"max_age_minutes": 30, "min_liquidity_usd": 5000, "max_liquidity_usd": 200000,
                          "min_mcap_usd": 0, "max_mcap_usd": 150000, "min_volume_h1_usd": 3000,
                          "min_buys_m5": 5, "min_buy_sell_ratio_h1": 1.2, "require_socials": False},
        "safety_filters": {"max_rugcheck_score": 70, "max_top10_holders_pct": 45,
                           "reject_mint_authority": True, "reject_freeze_authority": True,
                           "reject_danger_risks": False},
        "llm": {"min_confidence": 0.5},
        "tracking": {"target_multiple": 3.0},
        "pulse": {"soon_progress": 50, "columns": {
            "new": {"min_mcap": 4000, "max_age_min": 10, "require_socials": False, "max_risk": "HIGH"},
            "soon": {"min_progress": 50, "max_age_min": 60, "require_socials": False, "max_risk": "HIGH"},
            "migrated": {"min_mcap": 20000, "max_age_min": 120, "require_socials": False, "max_risk": "HIGH"}}},
    },
    "Momentum runner": {
        "_popis": "Už migrované coiny se silným objemem a převahou nákupů – jízda na trendu.",
        "scanner": {"sources": ["token_profiles", "token_boosts", "pumpfun"], "pumpfun_min_progress": 100},
        "basic_filters": {"max_age_minutes": 360, "min_liquidity_usd": 25000, "max_liquidity_usd": 1000000,
                          "min_mcap_usd": 0, "max_mcap_usd": 3000000, "min_volume_h1_usd": 100000,
                          "min_buys_m5": 25, "min_buy_sell_ratio_h1": 1.1, "require_socials": True},
        "safety_filters": {"max_rugcheck_score": 50, "max_top10_holders_pct": 30,
                           "reject_mint_authority": True, "reject_freeze_authority": True,
                           "reject_danger_risks": True},
        "llm": {"min_confidence": 0.6},
        "tracking": {"target_multiple": 2.0},
        "pulse": {"soon_progress": 80, "columns": {
            "new": {"min_mcap": 15000, "max_age_min": 15, "require_socials": True, "max_risk": "HIGH"},
            "soon": {"min_progress": 80, "max_age_min": 120, "require_socials": True, "max_risk": "MEDIUM"},
            "migrated": {"min_mcap": 100000, "max_age_min": 360, "require_socials": True, "max_risk": "MEDIUM"}}},
    },
}

PRESET_SECTIONS = ("scanner", "basic_filters", "safety_filters", "llm", "tracking", "pulse")


def custom_presets() -> dict[str, dict]:
    out = {}
    if PRESETS_DIR.exists():
        for p in sorted(PRESETS_DIR.glob("*.yaml")):
            try:
                out[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                pass
    return out


def all_presets() -> dict[str, dict]:
    return {**BUILTIN, **custom_presets()}


def apply(cfg: dict, name: str) -> dict:
    preset = {k: v for k, v in all_presets()[name].items() if not k.startswith("_")}
    out = deep_merge(cfg, preset)
    out["active_preset"] = name
    return out


def save_custom(name: str, cfg: dict, popis: str = ""):
    PRESETS_DIR.mkdir(exist_ok=True)
    data = {"_popis": popis or "Vlastní preset"}
    data.update({k: cfg[k] for k in PRESET_SECTIONS if k in cfg})
    safe = "".join(ch for ch in name if ch.isalnum() or ch in " _-").strip() or "muj_preset"
    (PRESETS_DIR / f"{safe}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return safe


def delete_custom(name: str) -> bool:
    p = PRESETS_DIR / f"{name}.yaml"
    if p.exists() and name not in BUILTIN:
        p.unlink()
        return True
    return False
