"""SQLite úložiště – coiny, predikce, snapshoty cen a výsledky."""
import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    address         TEXT UNIQUE NOT NULL,
    symbol          TEXT, name TEXT, pair_address TEXT, dex_id TEXT, url TEXT,
    source          TEXT,
    first_seen      REAL NOT NULL,
    pair_created_at REAL,
    stage           TEXT NOT NULL,      -- rejected_basic | rejected_safety | rejected_llm | passed | error
    reject_reason   TEXT,
    -- vstupní snapshot
    price0 REAL, mcap0 REAL, liq0 REAL, vol_h1_0 REAL, age_min0 REAL,
    -- bezpečnost
    rug_score REAL, mint_auth INTEGER, freeze_auth INTEGER, top10_pct REAL, rug_risks TEXT,
    -- LLM
    llm_verdict TEXT, llm_confidence REAL, llm_summary TEXT, llm_flags TEXT, llm_narrative TEXT,
    -- risk
    risk_score INTEGER, risk_label TEXT,
    alerted INTEGER DEFAULT 0,
    -- sledování výsledku
    last_price REAL, last_mcap REAL, last_liq REAL,
    peak_price REAL, peak_mult REAL, last_mult REAL, min_mult REAL,
    last_checked REAL, track_until REAL,
    outcome TEXT DEFAULT 'pending'      -- pending | hit | miss | rug | untracked
);
CREATE TABLE IF NOT EXISTS snapshots (
    coin_id INTEGER, ts REAL, price REAL, mcap REAL, liq REAL,
    FOREIGN KEY (coin_id) REFERENCES coins(id)
);
CREATE INDEX IF NOT EXISTS idx_coins_outcome ON coins(outcome);
CREATE INDEX IF NOT EXISTS idx_snap_coin ON snapshots(coin_id);
CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, usd REAL DEFAULT 0, calls INTEGER DEFAULT 0);
"""


@contextmanager
def connect(timeout: float = 10):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")   # čtení (GUI) neblokuje zápis (scanner)
        con.executescript(SCHEMA)
        from .gems import SCHEMA as GEMS_SCHEMA, migrate
        con.executescript(GEMS_SCHEMA)
        migrate(con)
        from .runners import SCHEMA as RUNNERS_SCHEMA
        con.executescript(RUNNERS_SCHEMA)
        from .watch import SCHEMA as WATCH_SCHEMA
        con.executescript(WATCH_SCHEMA)


def known_addresses(addresses: list[str]) -> set[str]:
    if not addresses:
        return set()
    with connect() as con:
        q = f"SELECT address FROM coins WHERE address IN ({','.join('?' * len(addresses))})"
        return {r["address"] for r in con.execute(q, addresses)}


def insert_coin(row: dict) -> int:
    row = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in row.items()}
    cols = ",".join(row)
    with connect() as con:
        cur = con.execute(f"INSERT OR IGNORE INTO coins ({cols}) VALUES ({','.join('?' * len(row))})",
                          list(row.values()))
        return cur.lastrowid


def update_coin(coin_id: int, **fields):
    sets = ",".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE coins SET {sets} WHERE id=?", [*fields.values(), coin_id])


def add_snapshot(coin_id: int, price, mcap, liq):
    with connect() as con:
        con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)", (coin_id, time.time(), price, mcap, liq))


def apply_tracking(updates: list[tuple[int, dict]], snaps: list[tuple]):
    """Všechny aktualizace trackeru v jedné transakci (dřív stovky commitů = zasekávání)."""
    with connect() as con:
        for coin_id, fields in updates:
            sets = ",".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE coins SET {sets} WHERE id=?", [*fields.values(), coin_id])
        if snaps:
            con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?)", snaps)


def active_tracked(now: float) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM coins WHERE outcome='pending' AND price0 IS NOT NULL").fetchall()


def list_coins(limit: int = 500, stage: str | None = None) -> list[sqlite3.Row]:
    with connect(timeout=3) as con:
        if stage:
            return con.execute("SELECT * FROM coins WHERE stage=? ORDER BY first_seen DESC LIMIT ?",
                               (stage, limit)).fetchall()
        return con.execute("SELECT * FROM coins ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()


def query(sql: str, params=()) -> list[sqlite3.Row]:
    with connect(timeout=3) as con:
        return con.execute(sql, params).fetchall()
