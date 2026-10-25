"""Pruebas con una respuesta real de OddsPapi (LaLiga, 25-oct-2026) recortada a mercados principales."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))

from vb.config import Config  # noqa: E402
from vb.db import connect  # noqa: E402
from vb.detector import detect, recheck_signals, store_signal  # noqa: E402
from vb.fair import fair_probs, margin, multiplicative, power, shin  # noqa: E402
from vb.parse import parse_fixture_odds  # noqa: E402
from vb.settle import closing_from_history, final_score, outcome_result, pnl_units  # noqa: E402


def load_sample():
    with open(os.path.join(HERE, "sample_laliga.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    markets = {}
    for m in d["markets"]:
        markets[m["marketId"]] = {"market_type": m["marketType"], "market_name": m["marketName"],
                                  "handicap": m.get("handicap"), "period": m.get("period"),
                                  "n_outcomes": m.get("marketLength"), "outcomes": m.get("outcomes", [])}
    return d["fixtures"], markets


def fake_ref_from_exchange(fx: dict, ref_name: str, now_iso: str) -> dict:
    """Construye una casa de referencia sintética a partir del exchange (lado back) con 2,5 % de margen."""
    fx = copy.deepcopy(fx)
    ex = fx["bookmakerOdds"].get("betfair-ex")
    if not ex:
        return fx
    ref = {"bookmakerIsActive": True, "suspended": False, "markets": {}}
    for mid, m in ex["markets"].items():
        outs = {}
        for oid, o in m["outcomes"].items():
            p = o["players"]["0"]
            if not p.get("price"):
                continue
            n = len(m["outcomes"])
            # cuota con margen: 1/(1/p + 0.025/n)
            price = round(1.0 / (1.0 / p["price"] + 0.025 / n), 3)
            outs[oid] = {"players": {"0": {"active": True, "changedAt": now_iso, "limit": 5000, "price": price,
                                           "mainLine": p.get("mainLine"), "exchangeMeta": None}}}
        if outs:
            ref["markets"][mid] = {"marketActive": True, "outcomes": outs}
    fx["bookmakerOdds"][ref_name] = ref
    return fx


class FairTests(unittest.TestCase):
    def test_methods_sum_to_one(self):
        for odds in ([1.591, 3.99, 6.40], [1.91, 1.95], [1.2, 6.5, 12.0]):
            for f in (multiplicative, power, shin):
                self.assertAlmostEqual(sum(f(odds)), 1.0, places=6)

    def test_shin_reduces_longshot(self):
        odds = [1.591, 3.99, 6.40]
        m, s = multiplicative(odds), shin(odds)
        self.assertLess(s[2], m[2])  # el perdedor pierde probabilidad
        self.assertGreater(s[0], m[0])

    def test_fair_probs_rules(self):
        self.assertEqual(fair_probs([1.91, 1.95]).method, "multiplicative")
        fr = fair_probs([1.591, 3.99, 6.40])
        self.assertEqual(fr.method, "shin")
        self.assertTrue(fr.ok)
        self.assertFalse(fair_probs([1.0, 3.0, 4.0]).ok)
        self.assertAlmostEqual(margin([2.0, 2.0]), 0.0)


class ParseTests(unittest.TestCase):
    def test_parse_sample(self):
        fixtures, markets = load_sample()
        now = datetime(2026, 10, 25, 11, 30, tzinfo=timezone.utc)
        snaps = parse_fixture_odds(fixtures[0], markets, now=now)
        self.assertTrue(snaps)
        books = {s.bookmaker for s in snaps}
        self.assertIn("bet365.es", books)
        self.assertIn("betfair-ex", books)
        x = [s for s in snaps if s.market_id == 101 and s.bookmaker == "bet365.es"]
        self.assertEqual(len(x), 3)
        self.assertTrue(all(s.age_s is not None and s.age_s >= 0 for s in x))
        ex = [s for s in snaps if s.bookmaker == "betfair-ex" and s.market_id == 101]
        self.assertTrue(any(s.back_size for s in ex))
        # totales/hándicaps: solo línea principal
        self.assertTrue(all(s.main_line for s in snaps if s.market_type in ("totals", "spreads")))


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(api_key="x", db_path=":memory:")
        self.conn = connect(":memory:")
        self.fixtures, self.markets = load_sample()
        for m in self.markets.items():
            pass
        for mid, m in self.markets.items():
            self.conn.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?)",
                              (mid, 10, m["market_type"], m["market_name"], m["handicap"], m["period"],
                               m["n_outcomes"], json.dumps(m["outcomes"])))
        self.conn.commit()

    def _fixture_row(self, fx):
        self.conn.execute("INSERT OR REPLACE INTO fixtures(fixture_id, sport_id, tournament_id, tournament_name, start_time, home, away) "
                          "VALUES (?,?,?,?,?,?,?)", (fx["fixtureId"], 10, 8, "LaLiga", fx["startTime"], "Casa", "Fuera"))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM fixtures WHERE fixture_id=?", (fx["fixtureId"],)).fetchone()

    def test_pipeline_with_synthetic_reference(self):
        fx = next(f for f in self.fixtures if "betfair-ex" in f["bookmakerOdds"])
        start = datetime.fromisoformat(fx["startTime"].replace("Z", "+00:00"))
        now = start - timedelta(minutes=60)
        fx2 = fake_ref_from_exchange(fx, self.cfg.ref_bookmaker, now.isoformat())
        # inflar la cuota del favorito en bet365.es para forzar una señal clara (dentro del tope de cuota)
        b = fx2["bookmakerOdds"]["bet365.es"]["markets"]["101"]["outcomes"]["101"]["players"]["0"]
        b["price"] = round(b["price"] * 1.15, 2)
        b["changedAt"] = now.isoformat()
        snaps = parse_fixture_odds(fx2, self.markets, now=now)
        row = self._fixture_row(fx)
        self.conn.execute("INSERT INTO sweeps(started_at) VALUES (?)", (now.isoformat(),))
        sweep_id = self.conn.execute("SELECT MAX(id) FROM sweeps").fetchone()[0]
        sigs = detect(self.conn, self.cfg, row, snaps, self.markets, sweep_id, now=now)
        self.assertTrue(sigs, "debería haber al menos una señal")
        s = next(x for x in sigs if x.bookmaker == "bet365.es" and x.market_id == 101 and x.outcome_id == 101)
        self.assertGreaterEqual(s.ev, self.cfg.ev_min_1x2)
        self.assertEqual(s.devig_method, "shin")
        sid = store_signal(self.conn, self.cfg, s, sweep_id)
        self.assertEqual(self.conn.execute("SELECT status FROM signals WHERE id=?", (sid,)).fetchone()[0], "paper")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bets WHERE signal_id=?", (sid,)).fetchone()[0], 1)
        # deduplicación
        sigs2 = detect(self.conn, self.cfg, row, snaps, self.markets, sweep_id, now=now)
        self.assertFalse(any(x.bookmaker == "bet365.es" and x.outcome_id == 101 for x in sigs2))
        # recheck: la cuota baja → no disponible
        b["price"] = round(b["price"] / 1.15, 2)
        snaps3 = parse_fixture_odds(fx2, self.markets, now=now + timedelta(minutes=10))
        n = recheck_signals(self.conn, snaps3)
        self.assertEqual(n, 1)
        self.assertEqual(self.conn.execute("SELECT still_available FROM signals WHERE id=?", (sid,)).fetchone()[0], 0)

    def test_stale_reference_blocks(self):
        fx = next(f for f in self.fixtures if "betfair-ex" in f["bookmakerOdds"])
        start = datetime.fromisoformat(fx["startTime"].replace("Z", "+00:00"))
        now = start - timedelta(minutes=60)
        old = (now - timedelta(minutes=20)).isoformat()
        fx2 = fake_ref_from_exchange(fx, self.cfg.ref_bookmaker, old)
        b = fx2["bookmakerOdds"]["bet365.es"]["markets"]["101"]["outcomes"]["103"]["players"]["0"]
        b["price"] = round(b["price"] * 1.2, 2)
        snaps = parse_fixture_odds(fx2, self.markets, now=now)
        row = self._fixture_row(fx)
        sigs = detect(self.conn, self.cfg, row, snaps, self.markets, 1, now=now)
        self.assertFalse(sigs, "referencia vieja: no debe haber señales")

    def test_too_close_to_kickoff(self):
        fx = next(f for f in self.fixtures if "betfair-ex" in f["bookmakerOdds"])
        start = datetime.fromisoformat(fx["startTime"].replace("Z", "+00:00"))
        now = start - timedelta(minutes=2)
        fx2 = fake_ref_from_exchange(fx, self.cfg.ref_bookmaker, now.isoformat())
        snaps = parse_fixture_odds(fx2, self.markets, now=now)
        row = self._fixture_row(fx)
        self.assertEqual(detect(self.conn, self.cfg, row, snaps, self.markets, 1, now=now), [])


class SettleTests(unittest.TestCase):
    def test_closing_from_history(self):
        start = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)
        hist = {"bookmakers": {"pinnacle+30": {"markets": {"101": {"outcomes": {
            "101": {"players": {"0": [
                {"createdAt": "2026-10-25T12:00:00Z", "price": 1.60},
                {"createdAt": "2026-10-25T12:55:00Z", "price": 1.58},
                {"createdAt": "2026-10-25T13:30:00Z", "price": 1.20},  # en directo: se ignora
            ]}}}}}}}}
        c = closing_from_history(hist, "pinnacle+30", start)
        self.assertEqual(c[101][101]["price"], 1.58)
        self.assertEqual(c[101][101]["opening"], 1.60)
        self.assertEqual(c[101][101]["n_changes"], 3)

    def test_results(self):
        outs = [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"}, {"outcomeId": 103, "outcomeName": "2"}]
        self.assertEqual(outcome_result("1x2", 101, None, outs, 2, 1), "win")
        self.assertEqual(outcome_result("1x2", 102, None, outs, 1, 1), "win")
        self.assertEqual(outcome_result("1x2", 103, None, outs, 2, 1), "lose")
        t = [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]
        self.assertEqual(outcome_result("totals", 1010, 2.5, t, 2, 1), "win")
        self.assertEqual(outcome_result("totals", 1011, 2.5, t, 2, 1), "lose")
        self.assertEqual(outcome_result("totals", 1010, 3.0, t, 2, 1), "push")
        self.assertAlmostEqual(pnl_units("win", 2.10, 1.0), 1.10)
        self.assertAlmostEqual(pnl_units("push", 2.10, 1.0), 0.0)

    def test_final_score_heuristic(self):
        self.assertEqual(final_score({"scores": {"0": {"participant1Score": 2, "participant2Score": 1},
                                                 "1": {"participant1Score": 1, "participant2Score": 0},
                                                 "2": {"participant1Score": 2, "participant2Score": 1}}}), (2, 1))
        self.assertEqual(final_score({"scores": {"1": {"participant1Score": 1, "participant2Score": 0},
                                                 "2": {"participant1Score": 1, "participant2Score": 1}}}), (2, 1))
        self.assertIsNone(final_score({}))
        real = {"fixtureId": "x", "scores": {"periods": {
            "result": {"participant1Score": 0, "participant2Score": 0},
            "p1": {"participant1Score": 0, "participant2Score": 0},
            "p2": {"participant1Score": 0, "participant2Score": 0}}}}
        self.assertEqual(final_score(real), (0, 0))
        et = {"scores": {"periods": {"result": {"participant1Score": 2, "participant2Score": 1},
                                     "p1": {"participant1Score": 1, "participant2Score": 0},
                                     "p2": {"participant1Score": 0, "participant2Score": 1}}}}
        self.assertEqual(final_score(et), (1, 1))  # 90 min, sin prórroga


if __name__ == "__main__":
    unittest.main(verbosity=2)
