"""Punto de entrada: python -m vb <comando>

Comandos:
  check                 comprueba clave, plan, casas y cuota (no gasta)
  tournaments           lista competiciones de fútbol con sus ids (1 consulta)
  markets               descarga el catálogo de mercados (1 consulta)
  fixtures              actualiza partidos de las competiciones configuradas (1 consulta por competición)
  run                   bucle permanente (recolector + detector + avisos)
  once                  una iteración del bucle (para probar)
  report                CLV por cortes de las señales en vivo
  staleness             edad media de las cuotas por casa
  backtest FROM TO      descarga histórico (gratis) y simula; p. ej. 2026-01-01 2026-10-20
  bt-report             CLV por cortes del backtest
  bt-export FICHERO     exporta señales del backtest a CSV
  bet ID STAKE_EUR      registra una apuesta real sobre la señal ID (para la fase 5)
  settle FIXTURE H A    liquida a mano un partido (marcador final)
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .alerts import Telegram
from .api import OddsPapi
from .collector import load_markets, refresh_fixtures, refresh_markets, run_forever
from .config import Config
from .db import connect


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def setup_logging(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    load_dotenv()
    cfg = Config()
    setup_logging(cfg.log_path)
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    conn = connect(cfg.db_path)
    api = OddsPapi(cfg.api_key, cfg.base_url, conn, cfg.monthly_quota, cfg.quota_reserve)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)

    if cmd == "check":
        cfg.validate()
        info = api.sync_quota_from_account()
        print("plan:", info.get("plan"), "| usadas:", info.get("used"), "/", info.get("limit"),
              "| válido hasta:", info.get("valid_until"))
        have = set(info.get("bookmakers", []))
        for b in cfg.all_bookmakers:
            print(("  OK  " if b in have else "  FALTA ") + b)
        print("competiciones:", cfg.tournament_ids)
        print("telegram:", "configurado" if tg.enabled else "NO configurado (los avisos irán al log)")
        print("consultas facturables registradas este mes (BD local):", api.used_this_month())
        return 0

    if cmd == "tournaments":
        cfg.validate()
        for t in api.tournaments(cfg.sport_id):
            if t.get("futureFixtures", 0) > 0 and (not args or any(a.lower() in (t.get("tournamentName", "") + t.get("categoryName", "")).lower() for a in args)):
                print(f"{t['tournamentId']:>6}  {t.get('categoryName',''):<16} {t.get('tournamentName','')}  (próximos: {t.get('futureFixtures')})")
        return 0

    if cmd == "markets":
        cfg.validate()
        print("mercados guardados:", refresh_markets(api, conn, cfg.sport_id))
        return 0

    if cmd == "fixtures":
        cfg.validate()
        print("partidos actualizados:", refresh_fixtures(api, conn, cfg))
        for r in conn.execute("SELECT start_time, tournament_name, home, away FROM fixtures WHERE stage='upcoming' ORDER BY start_time LIMIT 30"):
            print(f"  {r['start_time'][:16]}Z  {r['tournament_name']:<20} {r['home']} – {r['away']}")
        return 0

    if cmd in ("run", "once"):
        cfg.validate()
        run_forever(api, conn, cfg, tg, once=(cmd == "once"))
        return 0

    if cmd == "report":
        from .report import report
        print(report(conn, "signals"))
        return 0

    if cmd == "staleness":
        from .report import staleness_report
        print(staleness_report(conn))
        return 0

    if cmd == "backtest":
        cfg.validate()
        from .backtest import run_backtest
        if len(args) < 2:
            print("uso: backtest FROM TO [max_partidos]")
            return 1
        markets = load_markets(conn) or (refresh_markets(api, conn, cfg.sport_id) and load_markets(conn))
        fresh = "--fresh" in args
        args = [a for a in args if a != "--fresh"]
        mx = int(args[2]) if len(args) > 2 else 400
        print(run_backtest(api, conn, cfg, markets, args[0], args[1], mx, fresh=fresh))
        return 0

    if cmd == "probe-history":
        # Un partido por mes y competición: ¿hay histórico de la referencia y de una casa? (0 cuota salvo /fixtures)
        cfg.validate()
        from collections import defaultdict
        from .api import ApiError
        d_from = args[0] if args else "2026-01-01"
        d_to = args[1] if len(args) > 1 else "2026-10-24"
        for tid in cfg.tournament_ids:
            try:
                fx = api.fixtures(tid, d_from, d_to)
            except ApiError as e:
                print(f"competición {tid}: {e}")
                continue
            by_month = defaultdict(list)
            for f in fx:
                if f.get("startTime"):
                    by_month[f["startTime"][:7]].append(f)
            print(f"== competición {tid}: {len(fx)} partidos")
            for m in sorted(by_month):
                f = by_month[m][len(by_month[m]) // 2]
                res = []
                for bk in (cfg.ref_bookmaker, cfg.soft_bookmakers[0]):
                    try:
                        h = api.historical(f["fixtureId"], [bk])
                        mk = ((h.get("bookmakers") or {}).get(bk) or {}).get("markets") or {}
                        n = sum(len((o.get("players") or {}).get("0") or []) for mm in mk.values() for o in (mm.get("outcomes") or {}).values())
                        res.append(f"{bk}: {n} cambios")
                    except ApiError as e:
                        res.append(f"{bk}: {e.code}")
                print(f"  {m} ({len(by_month[m])} partidos) {f['startTime'][:10]} -> " + " | ".join(res))
        return 0

    if cmd == "replay":
        # Repite el detector actual sobre los barridos guardados (0 consultas). Uso: replay [desde YYYY-MM-DD]
        from datetime import datetime, timedelta
        from .collector import load_markets
        from .detector import detect
        from .parse import Snap, parse_ts
        markets = load_markets(conn)
        since = args[0] if args else "2000-01-01"
        sweeps = conn.execute("SELECT id, started_at FROM sweeps WHERE started_at>=? ORDER BY id", (since,)).fetchall()
        seen: dict[tuple, datetime] = {}
        total = 0
        for sw in sweeps:
            now = parse_ts(sw["started_at"])
            rows = conn.execute("SELECT * FROM snapshots WHERE sweep_id=?", (sw["id"],)).fetchall()
            by_fx: dict[str, list] = {}
            for r in rows:
                by_fx.setdefault(r["fixture_id"], []).append(Snap(
                    fixture_id=r["fixture_id"], bookmaker=r["bookmaker"], market_id=int(r["market_id"]),
                    market_type=r["market_type"], handicap=r["handicap"], outcome_id=int(r["outcome_id"]),
                    price=float(r["price"]), changed_at=r["changed_at"], age_s=r["age_s"], active=bool(r["active"]),
                    market_active=bool(r["market_active"]), main_line=bool(r["main_line"]), stake_limit=r["stake_limit"],
                    back_size=r["back_size"], lay_price=r["lay_price"], traded_volume=r["traded_volume"]))
            for fid, snaps in by_fx.items():
                fx = conn.execute("SELECT * FROM fixtures WHERE fixture_id=?", (fid,)).fetchone()
                if not fx:
                    continue
                for s in detect(conn, cfg, fx, snaps, markets, sw["id"], now=now):
                    key = (fid, s.bookmaker, s.market_id, s.outcome_id)
                    if key in seen and now - seen[key] < timedelta(minutes=cfg.signal_dedupe_min):
                        continue
                    seen[key] = now
                    total += 1
                    aviso = "" if (s.market_type in cfg.no_alert_market_types or
                                   (s.bookmaker == cfg.exchange_bookmaker and not cfg.exchange_alerts)) else "AVISO"
                    print(f"{sw['started_at'][11:16]}Z {fx['home']} - {fx['away']} | {s.bookmaker} {s.market_type} "
                          f"{s.outcome_label} @ {s.price:.2f} (justa {s.fair_odds:.2f}) EV {s.ev:+.1%} "
                          f"casa {s.soft_age_s or 0}s ref {s.ref_age_s or 0}s {aviso} {s.note}")
        print(f"barridos: {len(sweeps)} · señales que daría el detector actual: {total}")
        return 0

    if cmd == "bt-report":
        from .report import report
        print(report(conn, "bt_signals"))
        return 0

    if cmd == "bt-export":
        from .backtest import export_csv
        print("filas:", export_csv(conn, args[0] if args else "data/backtest.csv"))
        return 0

    if cmd == "bet":
        from datetime import datetime, timezone
        sid, stake = int(args[0]), float(args[1])
        s = conn.execute("SELECT price FROM signals WHERE id=?", (sid,)).fetchone()
        if not s:
            print("señal no encontrada")
            return 1
        conn.execute("INSERT INTO bets(signal_id, placed_at, is_paper, stake_units, stake_eur, price) VALUES (?,?,0,?,?,?)",
                     (sid, datetime.now(timezone.utc).isoformat(), 0.0, stake, s["price"]))
        conn.execute("UPDATE signals SET status='placed' WHERE id=?", (sid,))
        conn.commit()
        print("apuesta real registrada")
        return 0

    if cmd == "settle":
        from .settle import outcome_result, pnl_units
        import json
        from datetime import datetime, timezone
        fid, h, a = args[0], int(args[1]), int(args[2])
        conn.execute("UPDATE fixtures SET home_score=?, away_score=?, stage='settled', settled_at=? WHERE fixture_id=?",
                     (h, a, datetime.now(timezone.utc).isoformat(), fid))
        mk = {int(r["market_id"]): r for r in conn.execute("SELECT * FROM markets").fetchall()}
        for s in conn.execute("SELECT * FROM signals WHERE fixture_id=?", (fid,)).fetchall():
            outs = json.loads(mk[int(s["market_id"])]["outcomes_json"]) if int(s["market_id"]) in mk else []
            res = outcome_result(s["market_type"], int(s["outcome_id"]), s["handicap"], outs, h, a)
            bet = conn.execute("SELECT stake_units FROM bets WHERE signal_id=? ORDER BY id LIMIT 1", (s["id"],)).fetchone()
            conn.execute("UPDATE signals SET result=?, pnl_units=? WHERE id=?",
                         (res, pnl_units(res, s["price_net"], bet["stake_units"] if bet else 0.0), s["id"]))
        conn.commit()
        print("liquidado")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
