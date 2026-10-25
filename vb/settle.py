"""Cierre de la referencia (histórico gratuito), CLV y liquidación de resultados."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from .api import OddsPapi
from .config import Config
from .fair import fair_probs, net_price
from .parse import parse_ts

log = logging.getLogger("vb.settle")


def _series(entries: list[dict]) -> list[tuple[datetime, float]]:
    out = []
    for e in entries or []:
        ts = parse_ts(e.get("createdAt"))
        if ts and e.get("price"):
            out.append((ts, float(e["price"])))
    out.sort()
    return out


def closing_from_history(hist: dict, bookmaker: str, start: datetime, tolerance_s: int = 120) -> dict[int, dict[int, dict]]:
    """{market_id: {outcome_id: {price, changed_at, n_changes, opening}}} con el último precio antes del inicio."""
    limit = start + timedelta(seconds=tolerance_s)
    res: dict[int, dict[int, dict]] = {}
    bk = (hist.get("bookmakers") or {}).get(bookmaker) or {}
    for mid_s, m in (bk.get("markets") or {}).items():
        for oid_s, o in (m.get("outcomes") or {}).items():
            ser = _series((o.get("players") or {}).get("0") or [])
            before = [(t, p) for t, p in ser if t <= limit]
            if not before:
                continue
            t, p = before[-1]
            res.setdefault(int(mid_s), {})[int(oid_s)] = {
                "price": p, "changed_at": t.isoformat(), "n_changes": len(ser), "opening": ser[0][1],
            }
    return res


def fetch_closing_lines(api: OddsPapi, conn: sqlite3.Connection, cfg: Config, markets: dict[int, dict],
                        fixture: sqlite3.Row) -> int:
    """Descarga el histórico (gratis) de la referencia y de las casas con señal; guarda cierres y calcula CLV."""
    fid = fixture["fixture_id"]
    start = parse_ts(fixture["start_time"])
    books = [cfg.ref_bookmaker]
    sig_books = [r[0] for r in conn.execute(
        "SELECT DISTINCT bookmaker FROM signals WHERE fixture_id=?", (fid,)).fetchall()]
    books += [b for b in sig_books if b not in books and b != cfg.exchange_bookmaker]
    n = 0
    for i in range(0, len(books), 3):
        chunk = books[i:i + 3]
        hist = api.historical(fid, chunk)
        for bk in chunk:
            closing = closing_from_history(hist, bk, start)
            for mid, outs in closing.items():
                mk = markets.get(mid, {})
                n_exp = mk.get("n_outcomes")
                fair = None
                if bk == cfg.ref_bookmaker and n_exp and len(outs) == n_exp:
                    ordered = sorted(outs.items())
                    fr = fair_probs([v["price"] for _, v in ordered], cfg.shin_power_max_diff)
                    if fr.probs:
                        fair = {oid: (p, fr.method) for (oid, _), p in zip(ordered, fr.probs)}
                for oid, v in outs.items():
                    fp, meth = (fair.get(oid) if fair else (None, None)) or (None, None)
                    conn.execute(
                        """INSERT INTO closing_lines(fixture_id, bookmaker, market_id, outcome_id, price, changed_at,
                             fair_prob, fair_odds, devig_method, n_changes, opening_price)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fixture_id, bookmaker, market_id, outcome_id)
                           DO UPDATE SET price=excluded.price, changed_at=excluded.changed_at, fair_prob=excluded.fair_prob,
                             fair_odds=excluded.fair_odds, devig_method=excluded.devig_method,
                             n_changes=excluded.n_changes, opening_price=excluded.opening_price""",
                        (fid, bk, mid, oid, v["price"], v["changed_at"], fp, (1.0 / fp if fp else None), meth,
                         v["n_changes"], v["opening"]),
                    )
                    n += 1
    conn.execute("UPDATE fixtures SET closing_fetched_at=?, stage='closed' WHERE fixture_id=?",
                 (datetime.now(timezone.utc).isoformat(), fid))
    conn.commit()
    compute_clv(conn, cfg, fid)
    return n


def compute_clv(conn: sqlite3.Connection, cfg: Config, fixture_id: str) -> int:
    rows = conn.execute("SELECT id, market_id, outcome_id, price_net FROM signals WHERE fixture_id=? AND clv IS NULL",
                        (fixture_id,)).fetchall()
    n = 0
    for r in rows:
        c = conn.execute(
            "SELECT price, fair_odds FROM closing_lines WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=?",
            (fixture_id, cfg.ref_bookmaker, r["market_id"], r["outcome_id"]),
        ).fetchone()
        if not c or not c["fair_odds"]:
            continue
        clv = r["price_net"] / c["fair_odds"] - 1.0
        conn.execute("UPDATE signals SET closing_ref_price=?, closing_fair_odds=?, clv=? WHERE id=?",
                     (c["price"], c["fair_odds"], clv, r["id"]))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- resultados
def final_score(scores: dict) -> tuple[int, int] | None:
    """Heurística sobre /scores: toma el periodo con mayor suma (tiempo completo si los periodos son acumulados);
    si hay varios periodos no acumulados, suma los periodos 1..n. Se guarda el JSON crudo para revisar."""
    per = (scores or {}).get("scores") or {}
    if not per:
        return None
    items = []
    for k, v in per.items():
        try:
            items.append((int(k), int(v.get("participant1Score")), int(v.get("participant2Score"))))
        except (TypeError, ValueError):
            continue
    if not items:
        return None
    items.sort()
    if any(k == 0 for k, _, _ in items):
        _, h, a = next(x for x in items if x[0] == 0)
        others = [(hh, aa) for k, hh, aa in items if k != 0]
        # si el periodo 0 es >= a cualquier otro en ambos marcadores, es el total
        if all(h >= hh and a >= aa for hh, aa in others):
            return h, a
    # suma de periodos distintos de 0
    hs = sum(hh for k, hh, _ in items if k != 0)
    as_ = sum(aa for k, _, aa in items if k != 0)
    return hs, as_


def outcome_result(market_type: str, outcome_id: int, handicap: float | None, outcomes: list[dict],
                   home: int, away: int) -> str:
    name = next((o.get("outcomeName") for o in outcomes if int(o.get("outcomeId", -1)) == outcome_id), str(outcome_id))
    if market_type == "1x2":
        winner = "1" if home > away else ("2" if away > home else "X")
        return "win" if name == winner else "lose"
    if market_type == "totals":
        total = home + away
        line = handicap or 0.0
        if name.lower().startswith("over"):
            return "win" if total > line else ("push" if total == line else "lose")
        return "win" if total < line else ("push" if total == line else "lose")
    if market_type == "spreads":
        # hándicap referido al equipo 1 (convención del catálogo). Cuartos: media apuesta; se simplifica a push parcial.
        line = handicap or 0.0
        diff = (home + line) - away if name == "1" else (away - line) - home
        if abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:
            # línea de cuarto: se liquida como media apuesta
            lo, hi = line - 0.25, line + 0.25
            r1 = outcome_result(market_type, outcome_id, lo, outcomes, home, away)
            r2 = outcome_result(market_type, outcome_id, hi, outcomes, home, away)
            if r1 == r2:
                return r1
            return "half-" + (r1 if r1 != "push" else r2)
        return "win" if diff > 0 else ("push" if diff == 0 else "lose")
    return "void"


def pnl_units(result: str, price: float, stake: float) -> float:
    if result == "win":
        return stake * (price - 1.0)
    if result == "lose":
        return -stake
    if result == "half-win":
        return stake * (price - 1.0) / 2.0
    if result == "half-lose":
        return -stake / 2.0
    return 0.0


def settle_fixture(api: OddsPapi, conn: sqlite3.Connection, fixture: sqlite3.Row) -> int:
    fid = fixture["fixture_id"]
    sc = api.scores(fid)
    fs = final_score(sc)
    if not fs:
        log.info("sin resultado aún para %s", fid)
        return 0
    home, away = fs
    conn.execute("UPDATE fixtures SET home_score=?, away_score=?, stage='settled', settled_at=? WHERE fixture_id=?",
                 (home, away, datetime.now(timezone.utc).isoformat(), fid))
    mk_rows = {int(r["market_id"]): r for r in conn.execute("SELECT * FROM markets").fetchall()}
    n = 0
    for s in conn.execute("SELECT * FROM signals WHERE fixture_id=? AND result IS NULL", (fid,)).fetchall():
        mk = mk_rows.get(int(s["market_id"]))
        outs = json.loads(mk["outcomes_json"]) if mk and mk["outcomes_json"] else []
        res = outcome_result(s["market_type"], int(s["outcome_id"]), s["handicap"], outs, home, away)
        bet = conn.execute("SELECT stake_units FROM bets WHERE signal_id=? ORDER BY id LIMIT 1", (s["id"],)).fetchone()
        stake = bet["stake_units"] if bet else 0.0
        conn.execute("UPDATE signals SET result=?, pnl_units=? WHERE id=?",
                     (res, pnl_units(res, s["price_net"], stake), s["id"]))
        n += 1
    conn.commit()
    return n
