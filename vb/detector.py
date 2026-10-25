"""Detector de valor: compara cada casa con el precio justo de la referencia.

Filtros (diseño v1, sección 4):
- referencia completa, activa y con edad <= ref_max_age_s
- referencia estable: ningún resultado movido > ref_move_threshold desde el barrido anterior
- Shin y power coherentes en 1X2
- EV dentro de [ev_min, ev_max], cuota dentro de [odds_min, odds_max]
- misma línea (mismo market_id ⇒ mismo hándicap/total)
- al menos `min_minutes_before_kickoff` antes del inicio
- sin señal repetida para (partido, casa, mercado, resultado) en `signal_dedupe_min`
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from .fair import ev as calc_ev, fair_probs, net_price
from .parse import Snap, outcome_label, parse_ts

log = logging.getLogger("vb.detector")


@dataclass
class Signal:
    fixture_id: str
    bookmaker: str
    market_id: int
    market_type: str
    handicap: float | None
    outcome_id: int
    outcome_label: str
    price: float
    price_net: float
    fair_prob: float
    fair_odds: float
    ev: float
    ref_price: float
    ref_changed_at: str | None
    ref_age_s: int | None
    soft_changed_at: str | None
    soft_age_s: int | None
    minutes_to_kickoff: float
    devig_method: str
    note: str = ""


def _group(snaps: list[Snap]) -> dict[tuple[str, int], dict[int, Snap]]:
    """(bookmaker, market_id) -> {outcome_id: Snap}"""
    g: dict[tuple[str, int], dict[int, Snap]] = {}
    for s in snaps:
        g.setdefault((s.bookmaker, s.market_id), {})[s.outcome_id] = s
    return g


def previous_ref_prices(conn: sqlite3.Connection, fixture_id: str, bookmaker: str,
                        market_id: int, before_sweep_id: int) -> dict[int, float]:
    rows = conn.execute(
        """SELECT outcome_id, price FROM snapshots
           WHERE fixture_id=? AND bookmaker=? AND market_id=? AND sweep_id<?
           ORDER BY sweep_id DESC LIMIT 6""",
        (fixture_id, bookmaker, market_id, before_sweep_id),
    ).fetchall()
    out: dict[int, float] = {}
    for r in rows:
        out.setdefault(int(r["outcome_id"]), float(r["price"]))
    return out


def recent_signal_exists(conn: sqlite3.Connection, fixture_id: str, bookmaker: str,
                         market_id: int, outcome_id: int, minutes: int) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    row = conn.execute(
        """SELECT 1 FROM signals WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=?
           AND created_at>=? LIMIT 1""",
        (fixture_id, bookmaker, market_id, outcome_id, since),
    ).fetchone()
    return row is not None


def detect(conn: sqlite3.Connection, cfg: Config, fixture: sqlite3.Row, snaps: list[Snap],
           markets: dict[int, dict], sweep_id: int, now: datetime | None = None) -> list[Signal]:
    now = now or datetime.now(timezone.utc)
    start = parse_ts(fixture["start_time"])
    if not start:
        return []
    mins_to_ko = (start - now).total_seconds() / 60.0
    if mins_to_ko < cfg.min_minutes_before_kickoff:
        return []

    g = _group(snaps)
    signals: list[Signal] = []
    ref_markets = {mid: outs for (bk, mid), outs in g.items() if bk == cfg.ref_bookmaker}

    for mid, ref_outs in ref_markets.items():
        mk = markets.get(mid, {})
        mtype = mk.get("market_type", "unknown")
        n_exp = mk.get("n_outcomes") or len(ref_outs)
        if len(ref_outs) != n_exp or n_exp not in (2, 3):
            continue
        ref_list = sorted(ref_outs.values(), key=lambda s: s.outcome_id)
        # referencia activa y fresca
        # NOTA: `marketActive` de la API es False en algunas casas con cuotas vigentes (bet365.es, 888sport);
        # su significado no está documentado, así que no bloquea: se guarda y se analiza en papel.
        if any(not s.active for s in ref_list):
            continue
        ages = [s.age_s for s in ref_list if s.age_s is not None]
        ref_age = max(ages) if ages else None
        if ref_age is None or ref_age > cfg.ref_max_age_s:
            continue
        # referencia estable respecto al barrido anterior
        prev = previous_ref_prices(conn, fixture["fixture_id"], cfg.ref_bookmaker, mid, sweep_id)
        moving = False
        for s in ref_list:
            p0 = prev.get(s.outcome_id)
            if p0 and abs(s.price / p0 - 1.0) > cfg.ref_move_threshold:
                moving = True
                break
        if moving:
            log.info("ref en movimiento %s mid=%s; se espera", fixture["fixture_id"], mid)
            continue
        fr = fair_probs([s.price for s in ref_list], cfg.shin_power_max_diff)
        if not fr.ok:
            log.info("ref descartada %s mid=%s: %s", fixture["fixture_id"], mid, fr.note)
            continue
        fair_by_outcome = {s.outcome_id: p for s, p in zip(ref_list, fr.probs)}
        is_1x2 = n_exp == 3
        ev_min = cfg.ev_min_1x2 if is_1x2 else cfg.ev_min_2way
        odds_max = cfg.odds_max_1x2 if is_1x2 else cfg.odds_max_2way

        for (bk, bmid), outs in g.items():
            if bmid != mid or bk == cfg.ref_bookmaker:
                continue
            is_ex = bk == cfg.exchange_bookmaker
            for oid, s in outs.items():
                if oid not in fair_by_outcome or not s.active:
                    continue
                if not is_ex and s.age_s is not None and s.age_s > cfg.soft_max_age_s:
                    continue  # cuota vieja en la API: probablemente ya no existe en la web
                pnet = net_price(s.price, cfg.exchange_commission) if is_ex else s.price
                if s.price < cfg.odds_min or s.price > odds_max:
                    continue
                e = calc_ev(fair_by_outcome[oid], pnet)
                if e < ev_min:
                    continue
                note = ""
                if e > cfg.ev_max:
                    note = f"EV {e:.1%} > máximo: posible error de cuota o referencia vieja; NO apostar"
                if recent_signal_exists(conn, fixture["fixture_id"], bk, mid, oid, cfg.signal_dedupe_min):
                    continue
                ref_s = ref_outs[oid]
                signals.append(Signal(
                    fixture_id=fixture["fixture_id"], bookmaker=bk, market_id=mid, market_type=mtype,
                    handicap=mk.get("handicap"), outcome_id=oid,
                    outcome_label=outcome_label(mtype, oid, mk.get("outcomes"), mk.get("handicap")),
                    price=s.price, price_net=pnet, fair_prob=fair_by_outcome[oid],
                    fair_odds=1.0 / fair_by_outcome[oid], ev=e, ref_price=ref_s.price,
                    ref_changed_at=ref_s.changed_at, ref_age_s=ref_s.age_s,
                    soft_changed_at=s.changed_at, soft_age_s=s.age_s,
                    minutes_to_kickoff=mins_to_ko, devig_method=fr.method, note=note,
                ))
    return signals


def store_signal(conn: sqlite3.Connection, cfg: Config, sig: Signal, sweep_id: int) -> int:
    status = "rejected" if sig.ev > cfg.ev_max else "paper"
    cur = conn.execute(
        """INSERT INTO signals(created_at, sweep_id, fixture_id, bookmaker, market_id, market_type, handicap,
             outcome_id, outcome_label, price, price_net, fair_prob, fair_odds, ev, ref_price, ref_changed_at,
             ref_age_s, soft_changed_at, soft_age_s, minutes_to_kickoff, devig_method, status, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(), sweep_id, sig.fixture_id, sig.bookmaker, sig.market_id,
         sig.market_type, sig.handicap, sig.outcome_id, sig.outcome_label, sig.price, sig.price_net,
         sig.fair_prob, sig.fair_odds, sig.ev, sig.ref_price, sig.ref_changed_at, sig.ref_age_s,
         sig.soft_changed_at, sig.soft_age_s, sig.minutes_to_kickoff, sig.devig_method, status, sig.note),
    )
    sid = int(cur.lastrowid)
    if status == "paper":
        conn.execute(
            "INSERT INTO bets(signal_id, placed_at, is_paper, stake_units, price) VALUES (?,?,1,?,?)",
            (sid, datetime.now(timezone.utc).isoformat(), cfg.paper_stake_units, sig.price),
        )
    conn.commit()
    return sid


def recheck_signals(conn: sqlite3.Connection, snaps: list[Snap], max_age_min: int = 25) -> int:
    """Marca si la cuota de cada señal reciente sigue disponible en este barrido."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).isoformat()
    rows = conn.execute(
        "SELECT id, fixture_id, bookmaker, market_id, outcome_id, price FROM signals "
        "WHERE recheck_at IS NULL AND created_at>=?", (since,)
    ).fetchall()
    if not rows:
        return 0
    idx = {(s.fixture_id, s.bookmaker, s.market_id, s.outcome_id): s for s in snaps}
    n = 0
    for r in rows:
        s = idx.get((r["fixture_id"], r["bookmaker"], int(r["market_id"]), int(r["outcome_id"])))
        if not s:
            continue
        avail = 1 if (s.active and s.price >= r["price"] - 1e-9) else 0
        conn.execute("UPDATE signals SET recheck_at=?, recheck_price=?, still_available=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), s.price, avail, r["id"]))
        n += 1
    conn.commit()
    return n
