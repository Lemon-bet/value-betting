"""Backtest sobre el histórico de OddsPapi (endpoint gratuito).

Para cada partido terminado de las competiciones configuradas:
1. Descarga la serie completa de cambios de cuota de la referencia y de las casas.
2. Reproduce el detector en cada instante en que cambia una cuota (de la referencia
   o de una casa) dentro de la ventana previa al inicio.
3. Calcula el CLV de cada señal contra el cierre de la referencia y cuánto tiempo
   estuvo disponible la cuota.

Limitaciones: no se sabe si la casa habría aceptado la apuesta ni a qué importe; las
series pueden tener huecos; la "edad" de la referencia es tiempo desde su último
cambio, no desde la última lectura. Reduce la fase de papel, no la sustituye.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

from .api import ApiError, OddsPapi
from .config import Config
from .fair import ev as calc_ev, fair_probs, net_price
from .parse import outcome_label, parse_ts
from .settle import _series

log = logging.getLogger("vb.backtest")

BT_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_fixtures (
    fixture_id TEXT PRIMARY KEY, tournament_id INTEGER, start_time TEXT, home TEXT, away TEXT,
    fetched_at TEXT, n_books INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS bt_signals (
    id INTEGER PRIMARY KEY, fixture_id TEXT, tournament_id INTEGER, start_time TEXT, at TEXT,
    minutes_to_kickoff REAL, bookmaker TEXT, market_id INTEGER, market_type TEXT, handicap REAL,
    outcome_id INTEGER, outcome_label TEXT, price REAL, price_net REAL, fair_prob REAL, fair_odds REAL,
    ev REAL, ref_price REAL, ref_age_s INTEGER, trigger TEXT, devig_method TEXT,
    available_min REAL, closing_ref_price REAL, closing_fair_odds REAL, clv REAL,
    ref_limit REAL, soft_age_s INTEGER, is_main INTEGER
);
CREATE INDEX IF NOT EXISTS ix_bt_signals_fx ON bt_signals(fixture_id);
"""


INACTIVE_SKIPS = [0]  # contador global de comparaciones descartadas por cuota suspendida


class Series:
    """Serie temporal ordenada de (t, price)."""

    def __init__(self, entries: list[dict]):
        self.pts = _series(entries)
        self.ts = [t for t, _ in self.pts]
        lim = []
        for e in entries or []:
            ts = parse_ts(e.get("createdAt"))
            if ts and e.get("price"):
                lim.append((ts, e.get("limit"), e.get("active", True) is not False))
        lim.sort(key=lambda x: x[0])
        self.lims = [l for _, l, _ in lim]
        self.acts = [a for _, _, a in lim]

    def active_at(self, t: datetime) -> bool:
        """False si la entrada vigente en t está marcada como suspendida (active=false)."""
        i = bisect_right(self.ts, t) - 1
        if i < 0 or i >= len(self.acts):
            return False
        return self.acts[i]

    def limit_at(self, t: datetime) -> float | None:
        i = bisect_right(self.ts, t) - 1
        if i < 0 or i >= len(self.lims):
            return None
        return self.lims[i]

    def at(self, t: datetime) -> tuple[datetime, float] | None:
        i = bisect_right(self.ts, t) - 1
        return self.pts[i] if i >= 0 else None

    def next_change_after(self, t: datetime) -> datetime | None:
        i = bisect_right(self.ts, t)
        return self.ts[i] if i < len(self.ts) else None

    def changed_within(self, t: datetime, seconds: int, threshold: float) -> bool:
        """True si hubo un cambio relativo > threshold en los últimos `seconds` antes de t."""
        cur = self.at(t)
        if not cur:
            return False
        i = bisect_right(self.ts, t) - 1
        j = i
        while j - 1 >= 0 and (t - self.ts[j - 1]).total_seconds() <= seconds:
            j -= 1
        if j == i:
            return False
        base = self.pts[j][1]
        return abs(cur[1] / base - 1.0) > threshold


def _book_series(hist: dict, bookmaker: str, markets: dict[int, dict]) -> dict[int, dict[int, Series]]:
    out: dict[int, dict[int, Series]] = {}
    bk = (hist.get("bookmakers") or {}).get(bookmaker) or {}
    for mid_s, m in (bk.get("markets") or {}).items():
        mid = int(mid_s)
        mk = markets.get(mid)
        if not mk or mk.get("market_type") not in ("1x2", "totals", "spreads") or mk.get("period") not in ("fulltime", None, ""):
            continue
        for oid_s, o in (m.get("outcomes") or {}).items():
            ser = Series((o.get("players") or {}).get("0") or [])
            if ser.pts:
                out.setdefault(mid, {})[int(oid_s)] = ser
    return out


def _is_main(ref: dict[int, dict[int, Series]], markets: dict[int, dict], mid: int, t: datetime) -> int:
    """1 si `mid` es, en el instante t, la línea de Pinnacle más equilibrada de su tipo (aprox. línea principal)."""
    mt = markets.get(mid, {}).get("market_type")
    if mt == "1x2":
        return 1
    best, best_d = None, None
    for m2, outs in ref.items():
        if markets.get(m2, {}).get("market_type") != mt or len(outs) != 2:
            continue
        ps = [o.at(t) for o in outs.values()]
        if any(p is None for p in ps):
            continue
        d = abs(1.0 / ps[0][1] - 1.0 / ps[1][1])
        if best_d is None or d < best_d:
            best, best_d = m2, d
    return 1 if best == mid else 0


def simulate_fixture(cfg: Config, markets: dict[int, dict], fixture: dict, ref: dict[int, dict[int, Series]],
                     books: dict[str, dict[int, dict[int, Series]]]) -> list[dict]:
    start = parse_ts(fixture["start_time"])
    w_lo = start - timedelta(minutes=cfg.window_before_min)
    w_hi = start - timedelta(minutes=cfg.min_minutes_before_kickoff)
    signals: list[dict] = []
    last_sig: dict[tuple, datetime] = {}
    n_inactive = INACTIVE_SKIPS

    for mid, ref_outs in ref.items():
        mk = markets.get(mid, {})
        n_exp = mk.get("n_outcomes") or len(ref_outs)
        if len(ref_outs) != n_exp or n_exp not in (2, 3):
            continue
        is_1x2 = n_exp == 3
        ev_min = cfg.ev_min_1x2 if is_1x2 else cfg.ev_min_2way
        odds_max = cfg.odds_max_1x2 if is_1x2 else cfg.odds_max_2way
        oids = sorted(ref_outs)
        # cierre
        closing = [ref_outs[o].at(start + timedelta(seconds=120)) for o in oids]
        if any(c is None for c in closing):
            continue
        frc = fair_probs([c[1] for c in closing], cfg.shin_power_max_diff)
        closing_fair = {o: (1.0 / p if p > 0 else None) for o, p in zip(oids, frc.probs)} if frc.probs else {}

        for bk, bmk in books.items():
            outs = bmk.get(mid)
            if not outs:
                continue
            is_ex = bk == cfg.exchange_bookmaker
            # instantes de evaluación: cambios de la casa y de la referencia dentro de la ventana
            times = set()
            for o in oids:
                if o in outs:
                    times.update(t for t in outs[o].ts if w_lo <= t <= w_hi)
                times.update(t + timedelta(seconds=30) for t in ref_outs[o].ts if w_lo <= t <= w_hi)  # +30 s de retardo
            for t in sorted(times):
                refp = [ref_outs[o].at(t) for o in oids]
                if any(r is None for r in refp):
                    continue
                if cfg.bt_respect_active and not all(ref_outs[o].active_at(t) for o in oids):
                    continue  # referencia suspendida
                ref_age = max(int((t - r[0]).total_seconds()) for r in refp)
                lims = [ref_outs[o].limit_at(t) for o in oids]
                ref_limit = min(l for l in lims if l is not None) if any(l is not None for l in lims) else None
                if cfg.ref_min_limit and (ref_limit is None or ref_limit < cfg.ref_min_limit):
                    continue
                if any(ref_outs[o].changed_within(t, 600, cfg.ref_move_threshold) for o in oids):
                    continue  # referencia en movimiento
                fr = fair_probs([r[1] for r in refp], cfg.shin_power_max_diff)
                if not fr.ok:
                    continue
                fair = dict(zip(oids, fr.probs))
                for o in oids:
                    if o not in outs:
                        continue
                    cur = outs[o].at(t)
                    if not cur:
                        continue
                    if cfg.bt_respect_active and not outs[o].active_at(t):
                        n_inactive[0] += 1
                        continue  # cuota de la casa suspendida: no se podía apostar
                    price = cur[1]
                    if not is_ex and (t - cur[0]).total_seconds() > cfg.soft_max_age_s:
                        continue
                    pnet = net_price(price, cfg.exchange_commission) if is_ex else price
                    if price < cfg.odds_min or price > odds_max:
                        continue
                    e = calc_ev(fair[o], pnet)
                    if e < ev_min or e > cfg.ev_max:
                        continue
                    key = (bk, mid, o)
                    if key in last_sig and (t - last_sig[key]) < timedelta(minutes=cfg.signal_dedupe_min):
                        continue
                    last_sig[key] = t
                    nxt = outs[o].next_change_after(t)
                    avail = ((nxt or start) - t).total_seconds() / 60.0
                    cf = closing_fair.get(o)
                    signals.append({
                        "fixture_id": fixture["fixture_id"], "tournament_id": fixture.get("tournament_id"),
                        "start_time": fixture["start_time"], "at": t.isoformat(),
                        "minutes_to_kickoff": (start - t).total_seconds() / 60.0, "bookmaker": bk,
                        "market_id": mid, "market_type": mk.get("market_type"), "handicap": mk.get("handicap"),
                        "outcome_id": o, "outcome_label": outcome_label(mk.get("market_type", ""), o, mk.get("outcomes"), mk.get("handicap")),
                        "price": price, "price_net": pnet, "fair_prob": fair[o], "fair_odds": 1.0 / fair[o], "ev": e,
                        "ref_price": dict(zip(oids, refp))[o][1], "ref_age_s": ref_age, "ref_limit": ref_limit,
                        "soft_age_s": int((t - cur[0]).total_seconds()),
                        "is_main": _is_main(ref, markets, mid, t),
                        "trigger": "ref" if any(abs((t - timedelta(seconds=30) - rt).total_seconds()) < 1 for rt in ref_outs[o].ts) else "book",
                        "devig_method": fr.method, "available_min": avail,
                        "closing_ref_price": dict(zip(oids, closing))[o][1], "closing_fair_odds": cf,
                        "clv": (pnet / cf - 1.0) if cf else None,
                    })
    return signals


def run_backtest(api: OddsPapi, conn: sqlite3.Connection, cfg: Config, markets: dict[int, dict],
                 date_from: str, date_to: str, max_fixtures: int = 400, fresh: bool = False) -> dict:
    if fresh:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        for tbl in ("bt_fixtures", "bt_signals"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                conn.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_{stamp}")
        conn.execute("DROP INDEX IF EXISTS ix_bt_signals_fx")
        conn.commit()
        log.info("backtest nuevo: tablas anteriores archivadas con sufijo _%s", stamp)
    conn.executescript(BT_SCHEMA)
    books = [cfg.exchange_bookmaker, *cfg.soft_bookmakers]
    done = {r[0] for r in conn.execute("SELECT fixture_id FROM bt_fixtures").fetchall()}
    todo: list[dict] = []
    for tid in cfg.tournament_ids:
        try:
            fx = api.fixtures(tid, date_from, date_to)
        except ApiError as e:
            log.warning("fixtures %s: %s", tid, e)
            continue
        now = datetime.now(timezone.utc)
        for f in fx:
            st = parse_ts(f.get("startTime"))
            if f.get("fixtureId") in done or not st or st > now - timedelta(hours=3):
                continue
            todo.append({"fixture_id": f["fixtureId"], "tournament_id": f.get("tournamentId"),
                         "start_time": f["startTime"], "home": f.get("participant1Name"), "away": f.get("participant2Name")})
    todo.sort(key=lambda f: f["start_time"], reverse=True)  # lo más reciente primero
    todo = todo[:max_fixtures]
    log.info("backtest: %d partidos por descargar (de más reciente a más antiguo)", len(todo))
    n_sig = 0
    misses = 0
    for i, f in enumerate(todo, 1):
        if misses >= 15:
            log.info("backtest: 15 partidos seguidos sin histórico; se para aquí (fin de la retención)")
            break
        try:
            h_ref = api.historical(f["fixture_id"], [cfg.ref_bookmaker])
            ref = _book_series(h_ref, cfg.ref_bookmaker, markets)
            bseries: dict[str, dict] = {}
            soft = [b for b in books if b != cfg.exchange_bookmaker]
            for j in range(0, len(soft), 3):
                chunk = soft[j:j + 3]
                h = api.historical(f["fixture_id"], chunk)
                for b in chunk:
                    s = _book_series(h, b, markets)
                    if s:
                        bseries[b] = s
            # exchange: hay que pedir resultado a resultado; solo 1X2 para no alargar
            if cfg.exchange_bookmaker in books and cfg.market_1x2 in ref:
                ex: dict[int, dict[int, Series]] = {}
                for o in sorted(ref[cfg.market_1x2]):
                    try:
                        hx = api.historical(f["fixture_id"], [cfg.exchange_bookmaker], outcome_id=o)
                        sx = _book_series(hx, cfg.exchange_bookmaker, markets)
                        if cfg.market_1x2 in sx and o in sx[cfg.market_1x2]:
                            ex.setdefault(cfg.market_1x2, {})[o] = sx[cfg.market_1x2][o]
                    except ApiError:
                        pass
                if ex:
                    bseries[cfg.exchange_bookmaker] = ex
        except ApiError as e:
            log.warning("histórico %s: %s", f["fixture_id"], e)
            conn.execute("INSERT OR REPLACE INTO bt_fixtures VALUES (?,?,?,?,?,?,?,?)",
                         (f["fixture_id"], f["tournament_id"], f["start_time"], f["home"], f["away"],
                          datetime.now(timezone.utc).isoformat(), 0, str(e)[:200]))
            conn.commit()
            misses += 1
            continue
        misses = 0
        sigs = simulate_fixture(cfg, markets, f, ref, bseries) if ref else []
        for s in sigs:
            conn.execute(
                """INSERT INTO bt_signals(fixture_id, tournament_id, start_time, at, minutes_to_kickoff, bookmaker, market_id,
                   market_type, handicap, outcome_id, outcome_label, price, price_net, fair_prob, fair_odds, ev, ref_price,
                   ref_age_s, trigger, devig_method, available_min, closing_ref_price, closing_fair_odds, clv, ref_limit, soft_age_s, is_main)
                   VALUES (:fixture_id,:tournament_id,:start_time,:at,:minutes_to_kickoff,:bookmaker,:market_id,:market_type,
                   :handicap,:outcome_id,:outcome_label,:price,:price_net,:fair_prob,:fair_odds,:ev,:ref_price,:ref_age_s,
                   :trigger,:devig_method,:available_min,:closing_ref_price,:closing_fair_odds,:clv,:ref_limit,:soft_age_s,:is_main)""", s)
        conn.execute("INSERT OR REPLACE INTO bt_fixtures VALUES (?,?,?,?,?,?,?,?)",
                     (f["fixture_id"], f["tournament_id"], f["start_time"], f["home"], f["away"],
                      datetime.now(timezone.utc).isoformat(), len(bseries) + (1 if ref else 0), None))
        conn.commit()
        n_sig += len(sigs)
        if i % 10 == 0:
            log.info("backtest %d/%d partidos, %d señales", i, len(todo), n_sig)
    log.info("backtest: comparaciones descartadas por cuota suspendida: %d", INACTIVE_SKIPS[0])
    return {"fixtures": len(todo), "signals": n_sig, "descartes_suspendida": INACTIVE_SKIPS[0]}


def export_csv(conn: sqlite3.Connection, path: str) -> int:
    rows = conn.execute("SELECT * FROM bt_signals ORDER BY at").fetchall()
    if not rows:
        return 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(rows[0].keys())
        for r in rows:
            w.writerow(list(r))
    return len(rows)
