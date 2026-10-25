"""Informe de CLV por cortes, para señales en vivo (tabla signals) o de backtest (bt_signals)."""
from __future__ import annotations

import sqlite3
from math import sqrt


def _stats(rows: list[sqlite3.Row]) -> dict:
    clv = [r["clv"] for r in rows if r["clv"] is not None]
    evs = [r["ev"] for r in rows if r["ev"] is not None]
    n = len(clv)
    if n == 0:
        return {"n": len(rows), "n_clv": 0}
    mean = sum(clv) / n
    var = sum((x - mean) ** 2 for x in clv) / (n - 1) if n > 1 else 0.0
    se = sqrt(var / n) if n > 1 else float("nan")
    beat = sum(1 for x in clv if x > 0) / n
    return {"n": len(rows), "n_clv": n, "ev_mean": sum(evs) / len(evs) if evs else None,
            "clv_mean": mean, "clv_se": se, "clv_ci95": (mean - 1.96 * se, mean + 1.96 * se), "beat_close": beat}


def _fmt(s: dict) -> str:
    if s.get("n_clv", 0) == 0:
        return f"n={s['n']} (sin CLV)"
    lo, hi = s["clv_ci95"]
    ev = f"EV {s['ev_mean']:+.2%}" if s.get("ev_mean") is not None else ""
    return (f"n={s['n_clv']:>4} {ev} · CLV {s['clv_mean']:+.2%} [{lo:+.2%}, {hi:+.2%}] · "
            f"bate cierre {s['beat_close']:.0%}")


def report(conn: sqlite3.Connection, table: str = "signals", min_n: int = 20) -> str:
    extra = "" if table == "bt_signals" else " WHERE status IN ('paper','placed')"
    rows = conn.execute(f"SELECT * FROM {table}{extra}").fetchall()
    out = [f"== {table}: {_fmt(_stats(rows))}"]
    if not rows:
        return "\n".join(out)

    def cut(label: str, keyf):
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(str(keyf(r)), []).append(r)
        out.append(f"\n-- por {label}")
        for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            st = _stats(g)
            flag = "" if st.get("n_clv", 0) >= min_n else "  (muestra pequeña)"
            out.append(f"  {k:<22} {_fmt(st)}{flag}")

    cut("casa", lambda r: r["bookmaker"])
    cut("mercado", lambda r: r["market_type"])
    cut("antelación (min)", lambda r: _bucket(r["minutes_to_kickoff"], [15, 30, 60, 90]))
    cut("rango de cuota", lambda r: _bucket(r["price"], [1.5, 2.0, 2.5, 3.0, 4.0]))
    cut("EV", lambda r: _bucket(r["ev"] * 100, [3, 4, 5, 7, 10]))
    if table == "bt_signals":
        cut("disparo", lambda r: r["trigger"])
        cut("disponible (min)", lambda r: _bucket(r["available_min"], [1, 3, 5, 10, 30]))
    else:
        avail = [r for r in rows if r["still_available"] is not None]
        if avail:
            ok = sum(1 for r in avail if r["still_available"]) / len(avail)
            out.append(f"\n-- disponibilidad en el siguiente barrido: {ok:.0%} de {len(avail)}")
        res = [r for r in rows if r["pnl_units"] is not None]
        if res:
            out.append(f"-- resultado (no decide nada): {sum(r['pnl_units'] for r in res):+.2f} u en {len(res)} apuestas")
    return "\n".join(out)


def _bucket(x, edges: list[float]) -> str:
    if x is None:
        return "?"
    prev = None
    for e in edges:
        if x < e:
            return f"<{e:g}" if prev is None else f"{prev:g}-{e:g}"
        prev = e
    return f">={prev:g}"


def staleness_report(conn: sqlite3.Connection) -> str:
    """Distribución de la edad de las cuotas por casa en los barridos (radiografía del retardo)."""
    rows = conn.execute(
        """SELECT bookmaker, COUNT(*) n, AVG(age_s) avg_s,
                  SUM(CASE WHEN age_s<=120 THEN 1 ELSE 0 END)*1.0/COUNT(*) p2m,
                  SUM(CASE WHEN age_s<=600 THEN 1 ELSE 0 END)*1.0/COUNT(*) p10m,
                  SUM(CASE WHEN age_s>3600 THEN 1 ELSE 0 END)*1.0/COUNT(*) p1h
           FROM snapshots WHERE age_s IS NOT NULL GROUP BY bookmaker ORDER BY avg_s"""
    ).fetchall()
    out = ["== edad de las cuotas por casa (barridos en ventana)"]
    for r in rows:
        out.append(f"  {r['bookmaker']:<16} n={r['n']:>6} media {r['avg_s']/60:6.1f} min · ≤2 min {r['p2m']:.0%} · "
                   f"≤10 min {r['p10m']:.0%} · >1 h {r['p1h']:.0%}")
    return "\n".join(out)
