"""Prueba de humo del bucle con una API simulada (sin red)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))

from tests.test_vb import fake_ref_from_exchange, load_sample  # noqa: E402
from vb.alerts import Telegram  # noqa: E402
from vb.collector import fixtures_in_window, load_markets, post_match, refresh_fixtures, sweep  # noqa: E402
from vb.config import Config  # noqa: E402
from vb.db import connect  # noqa: E402
from vb.report import report, staleness_report  # noqa: E402


class FakeApi:
    """Devuelve la muestra con los partidos desplazados para que uno caiga en ventana."""

    def __init__(self, conn, cfg, fixtures, now):
        self.conn, self.cfg, self.now = conn, cfg, now
        self._fx = fixtures
        self.calls = []
        self.monthly_quota = 5000
        # el primer partido empieza dentro de 45 minutos
        base = datetime.fromisoformat(self._fx[0]["startTime"].replace("Z", "+00:00"))
        self.shift = (now + timedelta(minutes=45)) - base

    def _shifted(self):
        out = []
        for f in self._fx:
            st = datetime.fromisoformat(f["startTime"].replace("Z", "+00:00")) + self.shift
            g = dict(f)
            g["startTime"] = st.isoformat().replace("+00:00", "Z")
            out.append(g)
        return out

    def remaining(self):
        return 4000

    def used_this_month(self):
        return 1000

    def fixtures(self, tid, d_from, d_to):
        self.calls.append("fixtures")
        return [dict(f, participant1Name=f"Casa{i}", participant2Name=f"Fuera{i}", tournamentName="LaLiga")
                for i, f in enumerate(self._shifted())]

    def odds_by_tournaments(self, bookmaker, tids):
        self.calls.append(f"obt:{bookmaker}")
        out = []
        for f in self._shifted():
            g = fake_ref_from_exchange(f, self.cfg.ref_bookmaker, self.now.isoformat()) if bookmaker == self.cfg.ref_bookmaker else f
            bo = g["bookmakerOdds"].get(bookmaker)
            if not bo:
                continue
            # refrescar marcas de tiempo para que no parezcan viejas
            h = json.loads(json.dumps(bo))
            for m in h["markets"].values():
                for o in m["outcomes"].values():
                    o["players"]["0"]["changedAt"] = (self.now - timedelta(seconds=40)).isoformat()
            # inflar una cuota de paf.es para forzar una señal
            if bookmaker == "paf.es" and "101" in h["markets"]:
                p = h["markets"]["101"]["outcomes"]["101"]["players"]["0"]
                p["price"] = round(p["price"] * 1.15, 2)
            out.append({"fixtureId": g["fixtureId"], "startTime": g["startTime"], "bookmakerOdds": {bookmaker: h}})
        return out

    def historical(self, fid, books, outcome_id=None):
        self.calls.append("hist")
        res = {"fixtureId": fid, "bookmakers": {}}
        for bk in books:
            if bk == self.cfg.ref_bookmaker:
                res["bookmakers"][bk] = {"markets": {"101": {"outcomes": {
                    "101": {"players": {"0": [{"createdAt": (self.now - timedelta(hours=5)).isoformat(), "price": 1.55}]}},
                    "102": {"players": {"0": [{"createdAt": (self.now - timedelta(hours=5)).isoformat(), "price": 4.20}]}},
                    "103": {"players": {"0": [{"createdAt": (self.now - timedelta(hours=5)).isoformat(), "price": 6.50}]}},
                }}}}
        return res

    def scores(self, fid):
        self.calls.append("scores")
        return {"fixtureId": fid, "scores": {"0": {"participant1Score": 2, "participant2Score": 0}}}


class SmokeTest(unittest.TestCase):
    def test_full_cycle(self):
        cfg = Config(api_key="x", db_path=":memory:")
        conn = connect(":memory:")
        fixtures, markets = load_sample()
        for mid, m in markets.items():
            conn.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?)",
                         (mid, 10, m["market_type"], m["market_name"], m["handicap"], m["period"],
                          m["n_outcomes"], json.dumps(m["outcomes"])))
        conn.commit()
        now = datetime.now(timezone.utc)
        api = FakeApi(conn, cfg, fixtures, now)
        tg = Telegram("", "")
        n = refresh_fixtures(api, conn, cfg)
        self.assertGreater(n, 0)
        window = fixtures_in_window(conn, cfg, now)
        self.assertTrue(window)
        res = sweep(api, conn, cfg, load_markets(conn), tg, window)
        self.assertEqual(res["requests"], len(cfg.all_bookmakers))
        self.assertGreater(res["snapshots"], 0)
        self.assertGreater(res["signals"], 0, "la cuota inflada de paf.es debe producir señal")
        sig = conn.execute("SELECT * FROM signals WHERE bookmaker='paf.es' LIMIT 1").fetchone()
        self.assertIsNotNone(sig)
        # simular que el partido ya ha empezado hace 4 h
        conn.execute("UPDATE fixtures SET start_time=?", ((now - timedelta(hours=4)).isoformat(),))
        conn.commit()
        post_match(api, conn, cfg, load_markets(conn))
        sig = conn.execute("SELECT * FROM signals WHERE id=?", (sig["id"],)).fetchone()
        self.assertIsNotNone(sig["clv"], "el CLV debe calcularse con el cierre")
        self.assertIn(sig["result"], ("win", "lose", "push"))
        print("\n" + report(conn, "signals"))
        print(staleness_report(conn))
        print("llamadas:", api.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
