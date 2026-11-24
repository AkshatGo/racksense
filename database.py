"""SQLite persistence layer for RackSense.

Plain sqlite3 (WAL) with small helpers; the schema is created on first run and
seeded with a demo shop so the dashboard is never empty.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "RACKSENSE_DB", os.path.join(os.path.dirname(__file__), "data", "racksense.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS racks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT '',
    capacity_g REAL NOT NULL DEFAULT 25000,
    tare_g REAL NOT NULL DEFAULT 0,
    avg_item_g REAL NOT NULL DEFAULT 200,
    armed INTEGER NOT NULL DEFAULT 0,
    low_stock_pct REAL NOT NULL DEFAULT 20,
    device_key TEXT UNIQUE NOT NULL,
    weight_g REAL NOT NULL DEFAULT 0,
    cells_g TEXT NOT NULL DEFAULT '[0,0,0,0]',
    last_seen REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rack_id INTEGER NOT NULL,
    weight_g REAL NOT NULL,
    cells_g TEXT NOT NULL DEFAULT '[]',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_rack_ts ON readings(rack_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rack_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    prev_g REAL NOT NULL,
    curr_g REAL NOT NULL,
    delta_g REAL NOT NULL,
    qty INTEGER,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rack_id INTEGER NOT NULL,
    event_id INTEGER,
    message TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'high',
    state TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    ack_by TEXT,
    ack_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rack_id INTEGER NOT NULL,
    cmd TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    delivered_at REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            from security import hash_password

            now = time.time()
            conn.executemany(
                "INSERT INTO users (username, name, role, password_hash, created_at)"
                " VALUES (?,?,?,?,?)",
                [
                    ("owner", "Akshat Godika", "owner", hash_password("rack1234"), now),
                    ("staff", "Shop Staff", "staff", hash_password("staff1234"), now),
                ],
            )
        if conn.execute("SELECT COUNT(*) c FROM racks").fetchone()["c"] == 0:
            seed_racks(conn)


SEED_RACKS = [
    # code, name, location, tier, capacity_kg, tare_kg, avg_item_g, armed, fill%
    ("R-01", "Onion & Potato", "Aisle 1 · Front", "Tier 1", 30, 6.5, 200, 0, 0.68),
    ("R-02", "Tomato & Leafy", "Aisle 1 · Front", "Tier 2", 20, 4.2, 120, 0, 0.42),
    ("R-03", "Apple & Citrus", "Aisle 2 · Window", "Tier 1", 25, 5.0, 180, 0, 0.81),
    ("R-04", "Packaged Staples", "Aisle 3 · Back", "Tier 1", 40, 7.8, 500, 1, 0.60),
    ("R-05", "Dairy & Cold", "Aisle 3 · Back", "Tier 2", 15, 3.1, 250, 1, 0.35),
    ("R-06", "Seasonal Reserve", "Store room", "Tier 1", 50, 9.0, 350, 0, 0.12),
]


def seed_racks(conn: sqlite3.Connection) -> None:
    now = time.time()
    for code, name, loc, tier, cap_kg, tare_kg, avg_g, armed, fill in SEED_RACKS:
        cap_g = cap_kg * 1000
        tare_g = tare_kg * 1000
        stock = round(cap_g * fill)
        cells = _split_cells(stock)
        cur = conn.execute(
            "INSERT INTO racks (code,name,location,tier,capacity_g,tare_g,avg_item_g,"
            "armed,device_key,weight_g,cells_g,last_seen,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code,
                name,
                loc,
                tier,
                cap_g,
                tare_g,
                avg_g,
                armed,
                "rk_" + secrets.token_hex(8),
                stock,
                json.dumps(cells),
                now,
                now,
            ),
        )
        rack_id = cur.lastrowid
        # a little history so charts and the log are not empty on first load
        for i in range(24):
            ts = now - (24 - i) * 300
            w = max(0, stock + (i - 12) * 180)
            conn.execute(
                "INSERT INTO readings (rack_id, weight_g, cells_g, ts) VALUES (?,?,?,?)",
                (rack_id, w, json.dumps(_split_cells(w)), ts),
            )
        conn.execute(
            "INSERT INTO readings (rack_id, weight_g, cells_g, ts) VALUES (?,?,?,?)",
            (rack_id, stock, json.dumps(cells), now),
        )


def _split_cells(total: float) -> list[int]:
    """Give the four corner cells a believable uneven share of the load."""
    weights = (0.28, 0.25, 0.24, 0.23)
    out = [int(total * w) for w in weights]
    out[0] += int(total - sum(out))
    return out


def new_device_key() -> str:
    return "rk_" + secrets.token_hex(8)
