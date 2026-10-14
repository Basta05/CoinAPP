import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
PRESETS_DIR = ROOT / "presets"
DB_PATH = ROOT / "data" / "coinscan.db"

HEADER = ("# CoinScan – nastavení. Upravuj ideálně v appce (záložka Nastavení),\n"
          "# ruční úpravy tady fungují taky.\n")


def load_config() -> dict:
    load_dotenv(ROOT / ".env", override=True)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()
