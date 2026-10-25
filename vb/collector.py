"""Bucle principal: partidos → barridos en ventana → detector → avisos → cierre → liquidación."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .alerts import Telegram, format_signal
from .api import ApiError, OddsPapi, QuotaExhausted
from .config import Config
from .db import kv_get, kv_set
from .detector import detect, recheck_signals, store_signal
from .parse import Snap, parse_fixture_meta, parse_fixture_odds, parse_ts
from .settle import fetch_closing_lines, settle_fixture

log = logging.getLogger("vb.collector")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- catálogo de mercados
def load_markets(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT * FROM markets").fetchall()
    out = {}
    for r in rows:
        out[int(r["market_id"])] = {
            "market_type": r["market_type"], "market_name": r["market_name"], "handicap": r["handicap"],
            "period": r["period"], "n_outcomes": r["n_outcomes"],
            "outcomes": json.loads(r["outcomes_json"]) if r["outcomes_json"] else [],
        }
    return out


def refresh_markets(api: OddsPapi, conn: sqlite3.Connection, sport_id: int) -> int:
    data = api.markets(sport_id)
    n = 0
    for m in data:
        conn.execute(
            """INSERT INTO markets(market_id, sport_id, market_type, market_name, handicap, period, n_outcomes, outcomes_json)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(market_id) DO UPDATE SET
               market_type=excluded.market_type, market_name=excluded.market_name, handicap=excluded.handicap,
               period=excluded.period, n_outcomes=excluded.n_outcomes, outcomes_json=excluded.outcomes_json""",
            (m["marketId"], m.get("sportId"), m.get("marketType"), m.get("marketName"), m.get("handicap"),
             m.get("period"), m.get("marketLength"), json.dumps(m.get("outcomes") or [])),
        )
        n += 1
    conn.commit()
    kv_set(conn, "markets_refreshed_at", utcnow().isoformat())
    return n


# ---------------------------------------------------------------- partidos
def refresh_fixtures(api: OddsPapi, conn: sqlite3.Connection, cfg: Config) -> int:
    today = utcnow().date()
    d_from = (today - timedelta(days=1)).isoformat()
    d_to = (today + timedelta(days=cfg.fixtures_horizon_days)).isoformat()
    n = 0
    for tid in cfg.tournament_ids:
        try:
            fx = api.fixtures(tid, d_from, d_to)
        except ApiError as e:
            log.warning("fixtures %s: %s", tid, e)
            continue
        for f in fx:
            m = parse_fixture_meta(f)
            if not m["fixture_id"] or not m["start_time"]:
                continue
            conn.execute(
                """INSERT INTO fixtures(fixture_id, sport_id, tournament_id, tournament_name, start_time, home, away,
                     status_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fixture_id) DO UPDATE SET start_time=excluded.start_time, home=excluded.home,
                     away=excluded.away, status_id=excluded.status_id, tournament_name=excluded.tournament_name,
                     updated_at=excluded.updated_at""",
                (m["fixture_id"], m["sport_id"], m["tournament_id"], m["tournament_name"], m["start_time"],
                 m["home"], m["away"], m["status_id"], m["updated_at"]),
            )
            n += 1
    conn.commit()
    kv_set(conn, "fixtures_refreshed_at", utcnow().isoformat())
    return n


def fixtures_in_window(conn: sqlite3.Connection, cfg: Config, now: datetime) -> list[sqlite3.Row]:
    lo = (now - timedelta(minutes=cfg.window_after_min)).isoformat()
    hi = (now + timedelta(minutes=cfg.window_before_min)).isoformat()
    return conn.execute(
        "SELECT * FROM fixtures WHERE start_time BETWEEN ? AND ? AND stage='upcoming' ORDER BY start_time",
        (lo, hi),
    ).fetchall()


# ---------------------------------------------------------------- barrido
def sweep(api: OddsPapi, conn: sqlite3.Connection, cfg: Config, markets: dict[int, dict],
          tg: Telegram, window: list[sqlite3.Row]) -> dict:
    now = utcnow()
    cur = conn.execute("INSERT INTO sweeps(started_at) VALUES (?)", (now.isoformat(),))
    sweep_id = int(cur.lastrowid)
    conn.commit()
    wanted = {f["fixture_id"] for f in window}
    tournaments = sorted({int(f["tournament_id"]) for f in window})
    all_snaps: list[Snap] = []
    n_req = 0
    for bk in cfg.all_bookmakers:
        try:
            data = api.odds_by_tournaments(bk, tournaments)
            n_req += 1
        except QuotaExhausted:
            raise
        except ApiError as e:
            log.warning("barrido %s %s: %s", sweep_id, bk, e)
            continue
        for fx in data:
            if fx.get("fixtureId") not in wanted:
                continue
            all_snaps.extend(parse_fixture_odds(fx, markets, now=now))
    # guardar
    rows = [(sweep_id, now.isoformat(), s.fixture_id, s.bookmaker, s.market_id, s.market_type, s.handicap,
             s.outcome_id, s.price, s.changed_at, s.age_s, int(s.active), int(s.market_active), int(s.main_line),
             s.stake_limit, s.back_size, s.lay_price, s.traded_volume) for s in all_snaps]
    conn.executemany(
        """INSERT INTO snapshots(sweep_id, swept_at, fixture_id, bookmaker, market_id, market_type, handicap,
           outcome_id, price, changed_at, age_s, active, market_active, main_line, stake_limit, back_size,
           lay_price, traded_volume) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()

    # comprobar disponibilidad de señales anteriores
    recheck_signals(conn, all_snaps)

    # detectar
    n_sig = 0
    by_fx: dict[str, list[Snap]] = {}
    for s in all_snaps:
        by_fx.setdefault(s.fixture_id, []).append(s)
    for f in window:
        sigs = detect(conn, cfg, f, by_fx.get(f["fixture_id"], []), markets, sweep_id, now=now)
        for sig in sigs:
            sid = store_signal(conn, cfg, sig, sweep_id)
            n_sig += 1
            text = format_signal(sig, f, sid, cfg.paper_stake_units, sig.bookmaker == cfg.exchange_bookmaker)
            if tg.send(text):
                conn.execute("UPDATE signals SET alerted=1 WHERE id=?", (sid,))
    conn.execute(
        "UPDATE sweeps SET finished_at=?, n_requests=?, n_fixtures=?, n_snapshots=?, n_signals=?, ok=1 WHERE id=?",
        (utcnow().isoformat(), n_req, len(window), len(rows), n_sig, sweep_id),
    )
    conn.commit()
    return {"sweep_id": sweep_id, "requests": n_req, "fixtures": len(window), "snapshots": len(rows), "signals": n_sig}


# ---------------------------------------------------------------- ritmo adaptativo
def adaptive_interval_min(api: OddsPapi, conn: sqlite3.Connection, cfg: Config) -> int:
    """Alarga el intervalo si el consumo proyectado supera la cuota del mes."""
    now = utcnow()
    days_left = max(1.0, ((now.replace(day=28) + timedelta(days=4)).replace(day=1) - now).total_seconds() / 86400)
    remaining = api.remaining() - cfg.quota_reserve
    if remaining <= 0:
        return cfg.sweep_interval_max_min
    # horas de ventana previstas: partidos próximos × (ventana/60)
    upcoming = conn.execute(
        "SELECT COUNT(*) FROM fixtures WHERE stage='upcoming' AND start_time BETWEEN ? AND ?",
        (now.isoformat(), (now + timedelta(days=7)).isoformat()),
    ).fetchone()[0]
    # partidos coincidentes comparten barrido: se estima 1 ventana por cada 2 partidos
    windows_per_day = max(1.0, (upcoming / 2.0) / 7.0)
    reqs_per_sweep = len(cfg.all_bookmakers)
    sweeps_per_window = (cfg.window_before_min + cfg.window_after_min) / cfg.sweep_interval_min
    projected = windows_per_day * sweeps_per_window * reqs_per_sweep * days_left
    if projected <= remaining:
        return cfg.sweep_interval_min
    factor = projected / remaining
    return int(min(cfg.sweep_interval_max_min, max(cfg.sweep_interval_min, round(cfg.sweep_interval_min * factor))))


# ---------------------------------------------------------------- post-partido
def post_match(api: OddsPapi, conn: sqlite3.Connection, cfg: Config, markets: dict[int, dict]) -> None:
    now = utcnow()
    # 1) marcar empezados
    conn.execute("UPDATE fixtures SET stage='started' WHERE stage='upcoming' AND start_time < ?",
                 ((now - timedelta(minutes=cfg.window_after_min)).isoformat(),))
    conn.commit()
    # 2) cierre (gratis) 15 min tras el inicio, solo partidos con snapshots o señales
    rows = conn.execute(
        """SELECT f.* FROM fixtures f WHERE f.stage='started' AND f.closing_fetched_at IS NULL
           AND f.start_time < ? AND EXISTS (SELECT 1 FROM snapshots s WHERE s.fixture_id=f.fixture_id)""",
        ((now - timedelta(minutes=15)).isoformat(),),
    ).fetchall()
    for f in rows[:10]:
        try:
            fetch_closing_lines(api, conn, cfg, markets, f)
        except ApiError as e:
            log.warning("cierre %s: %s", f["fixture_id"], e)
            conn.execute("UPDATE fixtures SET closing_fetched_at=?, stage='closed' WHERE fixture_id=?",
                         (now.isoformat(), f["fixture_id"]))
            conn.commit()
    # 3) liquidar (cuesta 1 consulta) solo partidos con señales, 3 h después del inicio
    rows = conn.execute(
        """SELECT f.* FROM fixtures f WHERE f.stage='closed' AND f.settled_at IS NULL AND f.start_time < ?
           AND EXISTS (SELECT 1 FROM signals s WHERE s.fixture_id=f.fixture_id)""",
        ((now - timedelta(hours=3)).isoformat(),),
    ).fetchall()
    for f in rows[:10]:
        try:
            settle_fixture(api, conn, f)
        except (ApiError, QuotaExhausted) as e:
            log.warning("liquidación %s: %s", f["fixture_id"], e)
            break
    # partidos cerrados sin señales: nada que liquidar
    conn.execute(
        """UPDATE fixtures SET stage='settled', settled_at=? WHERE stage='closed' AND start_time < ?
           AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.fixture_id=fixtures.fixture_id)""",
        (now.isoformat(), (now - timedelta(hours=3)).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------- bucle
def run_forever(api: OddsPapi, conn: sqlite3.Connection, cfg: Config, tg: Telegram, once: bool = False) -> None:
    markets = load_markets(conn)
    if not markets:
        log.info("descargando catálogo de mercados")
        refresh_markets(api, conn, cfg.sport_id)
        markets = load_markets(conn)
    last_sweep = parse_ts(kv_get(conn, "last_sweep_at")) or datetime(2000, 1, 1, tzinfo=timezone.utc)
    last_heartbeat = parse_ts(kv_get(conn, "last_heartbeat_at")) or datetime(2000, 1, 1, tzinfo=timezone.utc)

    tg.send("vb: recolector arrancado")
    while True:
        now = utcnow()
        try:
            fr = parse_ts(kv_get(conn, "fixtures_refreshed_at"))
            if not fr or (now - fr) > timedelta(hours=cfg.fixtures_refresh_hours):
                n = refresh_fixtures(api, conn, cfg)
                log.info("partidos actualizados: %d", n)

            window = fixtures_in_window(conn, cfg, now)
            interval = adaptive_interval_min(api, conn, cfg)
            if window and (now - last_sweep) >= timedelta(minutes=interval):
                res = sweep(api, conn, cfg, markets, tg, window)
                last_sweep = now
                kv_set(conn, "last_sweep_at", now.isoformat())
                log.info("barrido %s: %d partidos, %d filas, %d señales, intervalo %d min, cuota restante %d",
                         res["sweep_id"], res["fixtures"], res["snapshots"], res["signals"], interval, api.remaining())

            post_match(api, conn, cfg, markets)

            if (now - last_heartbeat) >= timedelta(hours=cfg.heartbeat_hours):
                nxt = conn.execute("SELECT start_time, home, away FROM fixtures WHERE stage='upcoming' AND start_time>? "
                                   "ORDER BY start_time LIMIT 1", (now.isoformat(),)).fetchone()
                tg.send(f"vb vivo · cuota usada {api.used_this_month()}/{api.monthly_quota} · intervalo {interval} min · "
                        f"próximo: {nxt['home']}–{nxt['away']} {nxt['start_time'][:16]}Z" if nxt else
                        f"vb vivo · cuota usada {api.used_this_month()}/{api.monthly_quota} · sin partidos próximos")
                last_heartbeat = now
                kv_set(conn, "last_heartbeat_at", now.isoformat())
        except QuotaExhausted as e:
            log.error("cuota: %s", e)
            tg.send(f"vb: cuota agotada ({e}); en pausa 1 h")
            if once:
                return
            time.sleep(3600)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("error en el bucle: %s", e)
            tg.send(f"vb: error {type(e).__name__}: {str(e)[:200]}")
        if once:
            return
        time.sleep(30)
