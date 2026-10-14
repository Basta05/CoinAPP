"""PumpPortal websocket (zdarma): nové coiny a migrace v reálném čase, bez čekání na polling.
Používá jen free kanály subscribeNewToken + subscribeMigration a jen JEDNO spojení."""
import json
import threading
import time

from . import status

WS_URL = "wss://pumpportal.fun/api/data"
CURVE_TOKENS = 793_100_000
VIRTUAL_OFFSET = 1_073_000_000 - CURVE_TOKENS   # virtuální tokeny navíc v křivce


class PumpPortal:
    def __init__(self, on_new, on_migrate, log=lambda l, m: None):
        self.on_new, self.on_migrate, self.log = on_new, on_migrate, log
        self._stop = threading.Event()
        self.ws = None
        self.connected = False

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        try:
            import websocket  # websocket-client
        except ImportError:
            self.log("ERROR", "Chybí balíček websocket-client – spusť start.bat znovu (doinstaluje ho).")
            return
        delay = 3
        while not self._stop.is_set():
            try:
                self.ws = websocket.create_connection(WS_URL, timeout=30)
                self.ws.send(json.dumps({"method": "subscribeNewToken"}))
                self.ws.send(json.dumps({"method": "subscribeMigration"}))
                self.connected = True
                delay = 3
                while not self._stop.is_set():
                    msg = self.ws.recv()
                    if not msg:
                        continue
                    try:
                        d = json.loads(msg)
                    except ValueError:
                        continue
                    self._handle(d)
            except Exception as e:  # noqa: BLE001
                self.connected = False
                if not self._stop.is_set():
                    self.log("ERROR", f"PumpPortal websocket: {e} – znovu za {delay}s")
                    self._stop.wait(delay)
                    delay = min(delay * 2, 60)
        self.connected = False

    def _handle(self, d: dict):
        tx = d.get("txType")
        mint = d.get("mint")
        if not mint:
            return
        if tx == "create":
            v_tokens = float(d.get("vTokensInBondingCurve") or 0)
            real_left = max(0.0, v_tokens - VIRTUAL_OFFSET)
            self.on_new({
                "mint": mint, "name": d.get("name") or "", "symbol": d.get("symbol") or "",
                "market_cap_sol": float(d.get("marketCapSol") or 0),
                "progress": max(0.0, min(100.0, (1 - real_left / CURVE_TOKENS) * 100)),
                "sol_in_curve": max(0.0, float(d.get("vSolInBondingCurve") or 0) - 30),
                "creator": d.get("traderPublicKey") or "", "ts": time.time(),
            })
        elif tx == "migrate":
            self.on_migrate(mint)
