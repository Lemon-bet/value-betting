"""Cliente de OddsPapi v4.

- Respeta el enfriamiento por endpoint (429 RATE_LIMITED con retryMs).
- Lleva la cuenta de consultas facturables y se niega a gastar por debajo de
  la reserva mensual (salvo que se fuerce).
- Registra cada llamada en la tabla `api_calls` para auditar el consumo.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger("vb.api")

# Enfriamiento mínimo por endpoint según documentación / observado (segundos)
COOLDOWN = {
    "odds": 0.5,
    "odds-by-tournaments": 1.1,
    "fixtures": 2.1,
    "tournaments": 2.1,
    "historical-odds": 5.1,
    "scores": 1.1,
    "account": 0.3,
    "markets": 1.1,
    "bookmakers": 1.1,
}
# Endpoints que descuentan cuota (según "Requests & Quota")
BILLABLE = {"odds", "odds-by-tournaments", "fixtures", "fixture", "tournaments",
            "scores", "markets", "bookmakers", "sports", "players", "settlements", "participants"}


class QuotaExhausted(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, details: str):
        super().__init__(f"{status} {code}: {details}")
        self.status, self.code, self.details = status, code, details


class OddsPapi:
    def __init__(self, api_key: str, base_url: str, conn: sqlite3.Connection,
                 monthly_quota: int, quota_reserve: int, timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.conn = conn
        self.monthly_quota = monthly_quota
        self.quota_reserve = quota_reserve
        self.timeout = timeout
        self._last_call: dict[str, float] = {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "vb-collector/0.1"

    # ------------------------------------------------------------------ cuota
    def used_this_month(self) -> int:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE billable=1 AND called_at>=?", (start,)
        ).fetchone()
        return int(row[0])

    def remaining(self) -> int:
        return self.monthly_quota - self.used_this_month()

    def sync_quota_from_account(self) -> dict[str, Any]:
        """Lee /account (no cuenta) y devuelve plan, request_count y request_limit."""
        data = self.get("account", {})
        sub = next((s for s in data.get("subscriptions", []) if s.get("is_active")), None)
        if sub:
            self.monthly_quota = int(sub.get("request_limit") or self.monthly_quota)
            return {"plan": sub.get("plan"), "used": sub.get("request_count"),
                    "limit": sub.get("request_limit"), "valid_until": sub.get("valid_until"),
                    "bookmakers": sorted(sub.get("bookmakers", {}).keys())}
        return {}

    # ------------------------------------------------------------------ llamada
    def get(self, endpoint: str, params: dict[str, Any], force: bool = False) -> Any:
        billable = endpoint in BILLABLE
        if billable and not force and self.remaining() <= self.quota_reserve:
            raise QuotaExhausted(f"Cuota agotada salvo reserva ({self.remaining()} restantes)")

        # enfriamiento
        cd = COOLDOWN.get(endpoint, 1.1)
        wait = self._last_call.get(endpoint, 0) + cd - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        url = f"{self.base_url}/{endpoint}"
        q = {"apiKey": self.api_key, **params}
        t0 = time.monotonic()
        for attempt in range(4):
            try:
                r = self.session.get(url, params=q, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("red: %s (intento %d)", e, attempt + 1)
                time.sleep(2 * (attempt + 1))
                continue
            self._last_call[endpoint] = time.monotonic()
            if r.status_code == 429:
                try:
                    err = r.json().get("error", {})
                except Exception:
                    err = {}
                if err.get("code") == "REQUEST_LIMIT_EXCEEDED":
                    self._log_call(endpoint, params, r.status_code, billable, t0)
                    raise QuotaExhausted(err.get("details") or "límite mensual superado")
                retry = (err.get("retryMs") or 1500) / 1000.0
                time.sleep(retry + 0.1)
                continue
            self._log_call(endpoint, params, r.status_code, billable, t0)
            if r.status_code >= 400:
                try:
                    err = r.json().get("error", {})
                except Exception:
                    err = {"code": "HTTP", "details": r.text[:200]}
                raise ApiError(r.status_code, err.get("code", "HTTP"), err.get("details", ""))
            return r.json()
        raise ApiError(0, "RETRIES", f"sin respuesta válida de {endpoint}")

    def _log_call(self, endpoint: str, params: dict, status: int, billable: bool, t0: float) -> None:
        p = {k: v for k, v in params.items() if k != "apiKey"}
        self.conn.execute(
            "INSERT INTO api_calls(called_at, endpoint, params, status, billable, ms) VALUES (?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), endpoint, json.dumps(p, ensure_ascii=False),
             status, 1 if billable else 0, int((time.monotonic() - t0) * 1000)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ atajos
    def fixtures(self, tournament_id: int, date_from: str, date_to: str) -> list[dict]:
        data = self.get("fixtures", {"tournamentId": tournament_id, "from": date_from, "to": date_to})
        return data if isinstance(data, list) else data.get("data", [])

    def odds_by_tournaments(self, bookmaker: str, tournament_ids: list[int]) -> list[dict]:
        data = self.get("odds-by-tournaments",
                        {"bookmaker": bookmaker, "tournamentIds": ",".join(map(str, tournament_ids))})
        return data if isinstance(data, list) else data.get("data", [])

    def odds(self, fixture_id: str, bookmakers: list[str]) -> dict:
        return self.get("odds", {"fixtureId": fixture_id, "bookmakers": ",".join(bookmakers)})

    def historical(self, fixture_id: str, bookmakers: list[str], outcome_id: int | None = None) -> dict:
        p: dict[str, Any] = {"fixtureId": fixture_id, "bookmakers": ",".join(bookmakers[:3])}
        if outcome_id is not None:
            p["outcomeId"] = outcome_id
        return self.get("historical-odds", p)

    def scores(self, fixture_id: str) -> dict:
        return self.get("scores", {"fixtureId": fixture_id})

    def tournaments(self, sport_id: int) -> list[dict]:
        data = self.get("tournaments", {"sportId": sport_id})
        return data if isinstance(data, list) else data.get("data", [])

    def markets(self, sport_id: int) -> list[dict]:
        data = self.get("markets", {"sportId": sport_id})
        return data if isinstance(data, list) else data.get("data", [])
