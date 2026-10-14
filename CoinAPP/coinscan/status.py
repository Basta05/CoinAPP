"""Stav jednotlivých API – co funguje, co má pauzu, poslední chyba. Zobrazuje se v horní liště."""
import threading
import time

_lock = threading.Lock()
_state: dict[str, dict] = {}

NAMES = ("pump.fun", "Grafy", "RugCheck", "DexScreener", "AI")


def ok(name: str, msg: str = "", latency: float | None = None):
    with _lock:
        _state[name] = {"ok": True, "msg": msg, "ts": time.time(), "lat": latency}


def fail(name: str, msg: str):
    with _lock:
        _state[name] = {"ok": False, "msg": msg[:160], "ts": time.time(), "lat": None}


def get() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _state.items()}
