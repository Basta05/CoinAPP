"""OHLCV svíčky z pump.fun swap-api (zdarma, bez klíče). Funguje pro bonding curve i po migraci.

Endpoint vrací svíčky vzestupně OD `createdTs`, jen intervaly, kde byl obchod.
Proto se ptáme s createdTs = teď − okno a mezery doplňujeme plochou svíčkou."""
import threading
import time

import requests

from . import status
from .pumpfun import HEADERS

URL = "https://swap-api.pump.fun/v2/coins/{mint}/candles"
SUPPLY = 1_000_000_000          # pump.fun tokeny mají 1B supply -> mcap = cena × 1e9

# timeframe -> sekundy
TIMEFRAMES = {"1s": 1, "5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "1h": 3600}
_unsupported: set[str] = set()
_backoff_until = 0.0              # po 429 chvíli nevoláme (sdílí grafy + bot filtr)   # intervaly, které server odmítl -> skládáme z menších
_session = requests.Session()
_session.headers.update(HEADERS)


def backoff_left() -> float:
    return max(0.0, _backoff_until - time.time())


def _fetch_raw(mint: str, interval: str, since_ms: int, limit: int) -> list[list[float]]:
    global _backoff_until
    if backoff_left() > 0:
        raise requests.RequestException(f"swap-api pauza {backoff_left():.0f}s")
    t0 = time.time()
    r = _session.get(URL.format(mint=mint), params={
        "interval": interval, "limit": limit, "currency": "USD", "createdTs": int(since_ms)}, timeout=10)
    if r.status_code == 429:
        _backoff_until = time.time() + 20
        status.fail("Grafy", "rate limit – pauza 20s")
        raise requests.RequestException("rate limit")
    if r.status_code == 400:
        raise ValueError("unsupported")
    r.raise_for_status()
    status.ok("Grafy", latency=time.time() - t0)
    out = []
    for c in r.json() or []:
        try:
            out.append([c["timestamp"] / 1000, float(c["open"]) * SUPPLY, float(c["high"]) * SUPPLY,
                        float(c["low"]) * SUPPLY, float(c["close"]) * SUPPLY, float(c.get("volume") or 0)])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def aggregate(candles: list[list[float]], sec: int) -> list[list[float]]:
    out: list[list[float]] = []
    for t, o, h, l, c, v in candles:
        b = t - t % sec
        if out and out[-1][0] == b:
            last = out[-1]
            last[2] = max(last[2], h)
            last[3] = min(last[3], l)
            last[4] = c
            last[5] += v
        else:
            out.append([b, o, h, l, c, v])
    return out


def fill_gaps(candles: list[list[float]], sec: int, until: float | None = None, max_fill: int = 3000):
    """Doplní intervaly bez obchodu plochou svíčkou (jako na pump.fun)."""
    if not candles:
        return candles
    out = [candles[0]]
    for c in candles[1:]:
        prev = out[-1]
        n = int((c[0] - prev[0]) // sec) - 1
        if 0 < n <= max_fill:
            for i in range(1, n + 1):
                out.append([prev[0] + i * sec, prev[4], prev[4], prev[4], prev[4], 0.0])
        out.append(c)
    if until:
        while out[-1][0] + sec <= until and len(out) < len(candles) + max_fill:
            p = out[-1][4]
            out.append([out[-1][0] + sec, p, p, p, p, 0.0])
    return out


def fetch(mint: str, tf: str, created_ms: float | None = None, count: int = 300,
          since_ms: float | None = None) -> list[list[float]]:
    """Vrátí posledních ~count svíček [t, o, h, l, c, vol] v MCap USD."""
    sec = TIMEFRAMES[tf]
    now_ms = time.time() * 1000
    start = since_ms if since_ms is not None else now_ms - sec * 1000 * count
    if created_ms:
        start = max(start, created_ms - 60_000)
    interval = tf
    base_sec = sec
    if tf in _unsupported:
        # fallback: stáhnout nejbližší menší podporovaný a složit
        for cand in ("1m", "15s", "1s"):
            if TIMEFRAMES[cand] < sec and sec % TIMEFRAMES[cand] == 0 and cand not in _unsupported:
                interval, base_sec = cand, TIMEFRAMES[cand]
                break
    limit = min(1000, int(count * sec / base_sec) + 5)
    try:
        raw = _fetch_raw(mint, interval, start, limit)
    except ValueError:
        _unsupported.add(tf)
        if interval == tf and tf != "1s":
            return fetch(mint, tf, created_ms, count, since_ms)
        raise
    except requests.RequestException as e:
        status.fail("Grafy", str(e))
        raise
    if base_sec != sec:
        raw = aggregate(raw, sec)
    return raw


def recent_1s(mint: str, seconds: int = 600) -> list[list[float]]:
    """Sparse 1s svíčky za posledních `seconds` – vstup pro bot filtr (patterns.py)."""
    return _fetch_raw(mint, "1s", time.time() * 1000 - seconds * 1000, 1000)


class ChartFeed:
    """Drží svíčky vybraného coinu a timeframe, na pozadí je průběžně doplňuje."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mint: str | None = None
        self.tf = "15s"
        self.created_ms: float | None = None
        self.candles: list[list[float]] = []
        self.loading = False
        self.error = ""
        self.version = 0          # zvýší se při každé změně -> GUI ví, kdy překreslit
        self._stop = threading.Event()
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def set(self, mint: str, tf: str, created_ms: float | None):
        with self.lock:
            if mint == self.mint and tf == self.tf:
                return
            self.mint, self.tf, self.created_ms = mint, tf, created_ms
            self.candles = []
            self.loading = True
            self.error = ""
            self.version += 1
        self._wake.set()

    def get(self):
        with self.lock:
            return self.version, self.tf, [list(c) for c in self.candles], self.loading, self.error

    def push_price(self, mint: str, mcap: float):
        """Živá cena z pollingu mezi dotazy na svíčky – ať se poslední svíčka hýbe hned."""
        with self.lock:
            if mint != self.mint or not self.candles or not mcap:
                return
            sec = TIMEFRAMES[self.tf]
            b = time.time() - time.time() % sec
            last = self.candles[-1]
            if last[0] == b:
                last[2], last[3], last[4] = max(last[2], mcap), min(last[3], mcap), mcap
            elif b > last[0]:
                self.candles.append([b, last[4], max(last[4], mcap), min(last[4], mcap), mcap, 0.0])
            self.version += 1

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            with self.lock:
                mint, tf, created, have = self.mint, self.tf, self.created_ms, bool(self.candles)
                last_t = self.candles[-1][0] if self.candles else None
            if not mint:
                self._wake.wait(1)
                self._wake.clear()
                continue
            sec = TIMEFRAMES[tf]
            try:
                if not have:
                    data = fetch(mint, tf, created, count=400)
                    data = fill_gaps(data, sec, until=time.time())
                    with self.lock:
                        if (mint, tf) == (self.mint, self.tf):
                            self.candles = data[-3000:]
                            self.loading = False
                            self.error = "" if data else "zatím žádné obchody"
                            self.version += 1
                else:
                    # doplnit od poslední svíčky (přepíše i tu rozpracovanou)
                    new = fetch(mint, tf, created, count=50, since_ms=(last_t - sec) * 1000)
                    with self.lock:
                        if (mint, tf) == (self.mint, self.tf) and self.candles:
                            by_t = {c[0]: c for c in new}
                            cut = [c for c in self.candles if c[0] < min(by_t) ] if by_t else self.candles
                            merged = fill_gaps(cut + sorted(by_t.values()), sec, until=time.time())
                            self.candles = merged[-3000:]
                            self.version += 1
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.loading = False
                    self.error = f"graf nedostupný: {e}"[:120]
                    self.version += 1
            # 1s/5s graf obnovujeme každou sekundu, delší méně často
            self._wake.wait(1.0 if sec <= 15 else 2.0 if sec <= 60 else 5.0)
            self._wake.clear()
