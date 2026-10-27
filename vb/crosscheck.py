"""Cruce entre lo visto en vivo (snapshots) y el histórico de la API para los mismos partidos.

Responde a: ¿el histórico refleja lo que había en vivo? Si no, el backtest no es fiable.

1. Coherencia de precios: para cada cuota vista en vivo, el precio del histórico en ese mismo instante.
2. Coherencia de tiempos: para cada cuota vista en vivo con su `changed_at`, cuándo registra el histórico
   ese mismo precio (retraso histórico − vivo, en segundos; positivo = el histórico lo apunta más tarde).
3. Señales del backtest en esos partidos y qué se veía en vivo en el barrido siguiente.
"""
from __future__ import annotations

import logging
import sqlite3
from bisect import bisect_right
from datetime import timedelta
from statistics import median

from .api import ApiError, OddsPapi
from .backtest import _book_series, simulate_fixture
from .config import Config
from .parse import parse_ts

log = logging.getLogger("vb.crosscheck")


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def run_crosscheck(api: OddsPapi, conn: sqlite3.Connection, cfg: Config, markets: dict[int, dict]) -> str:
    out: list[str] = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    fixtures = [f for f in conn.execute(
        """SELECT DISTINCT f.* FROM fixtures f JOIN snapshots s ON s.fixture_id=f.fixture_id ORDER BY f.start_time""").fetchall()
        if parse_ts(f["start_time"]) and parse_ts(f["start_time"]) < now - timedelta(hours=2)]
    books = [cfg.ref_bookmaker, *[b for b in cfg.soft_bookmakers]]
    stats: dict[str, dict[str, list]] = {b: {"match": [], "delay": [], "diff": []} for b in books}
    sig_lines: list[str] = []
    for f in fixtures:
        fid = f["fixture_id"]
        series: dict[str, dict] = {}
        try:
            for i in range(0, len(books), 3):
                chunk = books[i:i + 3]
                h = api.historical(fid, chunk)
                for b in chunk:
                    series[b] = _book_series(h, b, markets)
        except ApiError as e:
            out.append(f"{f['home']}–{f['away']}: histórico no disponible ({e.code})")
            continue
        out.append(f"{f['start_time'][:16]} {f['home']}–{f['away']}: libros con histórico {sorted(k for k, v in series.items() if v)}")
        rows = conn.execute("SELECT * FROM snapshots WHERE fixture_id=? AND bookmaker IN (%s)" % ",".join("?" * len(books)),
                            (fid, *books)).fetchall()
        for r in rows:
            ser = (series.get(r["bookmaker"]) or {}).get(int(r["market_id"]), {}).get(int(r["outcome_id"]))
            if not ser or not ser.pts:
                continue
            t_sweep = parse_ts(r["swept_at"])
            h_at = ser.at(t_sweep)
            if h_at is None:
                continue
            live = float(r["price"])
            stats[r["bookmaker"]]["match"].append(1 if abs(h_at[1] - live) < 0.006 else 0)
            stats[r["bookmaker"]]["diff"].append(h_at[1] / live - 1.0)
            # retraso: primera entrada del histórico con ese precio en torno al changed_at en vivo
            t_live = parse_ts(r["changed_at"])
            if t_live:
                cand = [t for t, p in ser.pts if abs(p - live) < 0.006 and abs((t - t_live).total_seconds()) < 3 * 3600]
                if cand:
                    best = min(cand, key=lambda t: abs((t - t_live).total_seconds()))
                    stats[r["bookmaker"]]["delay"].append((best - t_live).total_seconds())
        # señales del backtest en este partido y lo visto en vivo
        ref = series.get(cfg.ref_bookmaker) or {}
        soft = {b: s for b, s in series.items() if b != cfg.ref_bookmaker and s}
        fx = {"fixture_id": fid, "tournament_id": f["tournament_id"], "start_time": f["start_time"]}
        sigs = simulate_fixture(cfg, markets, fx, ref, soft) if ref else []
        sweeps = conn.execute("SELECT DISTINCT swept_at FROM snapshots WHERE fixture_id=? ORDER BY swept_at", (fid,)).fetchall()
        sw_ts = [parse_ts(s["swept_at"]) for s in sweeps]
        for s in sigs:
            t = parse_ts(s["at"])
            i = bisect_right(sw_ts, t)
            nxt = sw_ts[i] if i < len(sw_ts) else None
            live_soft = live_ref = None
            if nxt:
                rs = conn.execute("SELECT price FROM snapshots WHERE fixture_id=? AND swept_at=? AND bookmaker=? AND market_id=? AND outcome_id=?",
                                  (fid, nxt.isoformat(), s["bookmaker"], s["market_id"], s["outcome_id"])).fetchone()
                rr = conn.execute("SELECT price FROM snapshots WHERE fixture_id=? AND swept_at=? AND bookmaker=? AND market_id=? AND outcome_id=?",
                                  (fid, nxt.isoformat(), cfg.ref_bookmaker, s["market_id"], s["outcome_id"])).fetchone()
                live_soft = rs["price"] if rs else None
                live_ref = rr["price"] if rr else None
            sig_lines.append(
                f"  {s['at'][11:19]}Z {f['home'][:12]}–{f['away'][:12]} {s['bookmaker']} {s['market_type']} {s['outcome_label']}: "
                f"histórico casa {s['price']:.2f} ref {s['ref_price']:.2f} EV {s['ev']:+.1%} dura {s['available_min']:.0f} min | "
                f"vivo en {nxt.strftime('%H:%M') if nxt else '—'}: casa {live_soft or '—'} ref {live_ref or '—'}")
    out.append("\n== coherencia histórico vs vivo (por casa)")
    for b, st in stats.items():
        if not st["match"]:
            out.append(f"  {b:<14} sin datos comparables")
            continue
        m = sum(st["match"]) / len(st["match"])
        d = st["delay"]
        out.append(f"  {b:<14} cuotas comparadas {len(st['match']):>5} · precio idéntico {m:.0%} · "
                   f"diferencia media {sum(st['diff'])/len(st['diff']):+.2%}"
                   + (f" · retraso del histórico: mediana {median(d):+.0f} s, p10 {_q(d, .1):+.0f} s, p90 {_q(d, .9):+.0f} s (n={len(d)})" if d else ""))
    out.append(f"\n== señales del backtest en estos partidos: {len(sig_lines)}")
    out.extend(sig_lines[:60])
    if len(sig_lines) > 60:
        out.append(f"  … y {len(sig_lines) - 60} más")
    return "\n".join(out)
