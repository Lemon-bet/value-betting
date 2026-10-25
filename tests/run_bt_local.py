"""Backtest local sobre los ficheros data/bt/slim_*.json descargados vía navegador."""
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_vb import load_sample  # noqa: E402
from vb.backtest import BT_SCHEMA, _book_series, simulate_fixture  # noqa: E402
from vb.config import Config  # noqa: E402
from vb.db import connect  # noqa: E402
from vb.report import report  # noqa: E402

YEAR = "2026-"


def to_hist(slim_books: dict) -> dict:
    out = {"bookmakers": {}}
    for bk, mk in slim_books.items():
        mm = {}
        for mid, outs in mk.items():
            oo = {}
            for oid, arr in outs.items():
                oo[oid] = {"players": {"0": [{"createdAt": YEAR + t + "Z", "price": p} for t, p in arr]}}
            mm[mid] = {"outcomes": oo}
        out["bookmakers"][bk] = {"markets": mm}
    return out


def main(db_path: str = "data/bt_local.sqlite"):
    cfg = Config(api_key="x")
    _, markets = load_sample()
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = connect(db_path)
    conn.executescript(BT_SCHEMA)
    total = 0
    for path in sorted(glob.glob("data/bt/slim_*.json")):
        for f in json.load(open(path)):
            hist = to_hist(f["books"])
            ref = _book_series(hist, cfg.ref_bookmaker, markets)
            books = {bk: _book_series(hist, bk, markets) for bk in f["books"] if bk != cfg.ref_bookmaker}
            fixture = {"fixture_id": f["id"], "tournament_id": 8, "start_time": f["t"]}
            sigs = simulate_fixture(cfg, markets, fixture, ref, books)
            for s in sigs:
                conn.execute(
                    """INSERT INTO bt_signals(fixture_id, tournament_id, start_time, at, minutes_to_kickoff, bookmaker, market_id,
                       market_type, handicap, outcome_id, outcome_label, price, price_net, fair_prob, fair_odds, ev, ref_price,
                       ref_age_s, trigger, devig_method, available_min, closing_ref_price, closing_fair_odds, clv)
                       VALUES (:fixture_id,:tournament_id,:start_time,:at,:minutes_to_kickoff,:bookmaker,:market_id,:market_type,
                       :handicap,:outcome_id,:outcome_label,:price,:price_net,:fair_prob,:fair_odds,:ev,:ref_price,:ref_age_s,
                       :trigger,:devig_method,:available_min,:closing_ref_price,:closing_fair_odds,:clv)""", s)
            conn.commit()
            total += len(sigs)
            print(f"{f['t'][:16]} {f['h']} - {f['a']}: {len(sigs)} señales")
    print("\nTOTAL señales:", total)
    print(report(conn, "bt_signals", min_n=10))
    return conn


if __name__ == "__main__":
    main()
