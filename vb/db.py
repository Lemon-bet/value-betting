"""Esquema SQLite y utilidades de acceso."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY,
    called_at TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    params TEXT,
    status INTEGER,
    billable INTEGER NOT NULL DEFAULT 1,
    ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_api_calls_at ON api_calls(called_at);

CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id TEXT PRIMARY KEY,
    sport_id INTEGER,
    tournament_id INTEGER,
    tournament_name TEXT,
    start_time TEXT NOT NULL,
    home TEXT,
    away TEXT,
    status_id INTEGER,
    stage TEXT NOT NULL DEFAULT 'upcoming',   -- upcoming | started | closed | settled | void
    closing_fetched_at TEXT,
    settled_at TEXT,
    home_score INTEGER,
    away_score INTEGER,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_fixtures_start ON fixtures(start_time);

CREATE TABLE IF NOT EXISTS markets (
    market_id INTEGER PRIMARY KEY,
    sport_id INTEGER,
    market_type TEXT,
    market_name TEXT,
    handicap REAL,
    period TEXT,
    n_outcomes INTEGER,
    outcomes_json TEXT
);

CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    n_requests INTEGER DEFAULT 0,
    n_fixtures INTEGER DEFAULT 0,
    n_snapshots INTEGER DEFAULT 0,
    n_signals INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    note TEXT
);

-- Una fila por (barrido, partido, casa, mercado, resultado). Guarda la cuota tal
-- como la devuelve la API, con la marca de tiempo del proveedor.
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    sweep_id INTEGER NOT NULL,
    swept_at TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market_id INTEGER NOT NULL,
    market_type TEXT,
    handicap REAL,
    outcome_id INTEGER NOT NULL,
    price REAL NOT NULL,
    changed_at TEXT,
    age_s INTEGER,
    active INTEGER,
    market_active INTEGER,
    main_line INTEGER,
    stake_limit REAL,
    back_size REAL,
    lay_price REAL,
    traded_volume REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_fx ON snapshots(fixture_id, bookmaker, market_id, outcome_id, swept_at);
CREATE INDEX IF NOT EXISTS ix_snap_sweep ON snapshots(sweep_id);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    sweep_id INTEGER,
    fixture_id TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market_id INTEGER NOT NULL,
    market_type TEXT,
    handicap REAL,
    outcome_id INTEGER NOT NULL,
    outcome_label TEXT,
    price REAL NOT NULL,            -- cuota ofrecida por la casa
    price_net REAL NOT NULL,        -- cuota neta (exchange: descontada comisión)
    fair_prob REAL NOT NULL,
    fair_odds REAL NOT NULL,
    ev REAL NOT NULL,
    ref_price REAL,
    ref_changed_at TEXT,
    ref_age_s INTEGER,
    soft_changed_at TEXT,
    soft_age_s INTEGER,
    minutes_to_kickoff REAL,
    devig_method TEXT,
    alerted INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'paper',   -- paper | placed | skipped | rejected
    note TEXT,
    -- comprobación en el barrido siguiente
    recheck_at TEXT,
    recheck_price REAL,
    still_available INTEGER,
    -- cierre y CLV
    closing_ref_price REAL,
    closing_fair_odds REAL,
    clv REAL,
    result TEXT,                    -- win | lose | push | void
    pnl_units REAL
);
CREATE INDEX IF NOT EXISTS ix_signals_fx ON signals(fixture_id);
CREATE INDEX IF NOT EXISTS ix_signals_created ON signals(created_at);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    placed_at TEXT NOT NULL,
    is_paper INTEGER NOT NULL DEFAULT 1,
    stake_units REAL NOT NULL,
    stake_eur REAL,
    price REAL NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS closing_lines (
    fixture_id TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market_id INTEGER NOT NULL,
    outcome_id INTEGER NOT NULL,
    price REAL NOT NULL,
    changed_at TEXT,
    fair_prob REAL,
    fair_odds REAL,
    devig_method TEXT,
    n_changes INTEGER,
    opening_price REAL,
    PRIMARY KEY (fixture_id, bookmaker, market_id, outcome_id)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # migraciones ligeras
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
    for c, t in (("settle_attempts", "INTEGER DEFAULT 0"), ("last_settle_at", "TEXT")):
        if c not in fcols:
            conn.execute(f"ALTER TABLE fixtures ADD COLUMN {c} {t}")
            conn.commit()
    if "ref_limit" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN ref_limit REAL")
        conn.commit()
    for c, t in (("verified_price", "REAL"), ("verified_at", "TEXT"), ("clv_real", "REAL")):
        if c not in cols:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {c} {t}")
            conn.commit()
    if "main_line" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN main_line INTEGER")
        conn.commit()
    return conn


def kv_get(conn: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    conn.commit()
