"""Parser de las respuestas de OddsPapi a filas de `snapshots`.

Estructura: fixture -> bookmakerOdds[bk] -> markets[mid] -> outcomes[oid] -> players["0"]
Solo se conservan los mercados que interesan: 1X2 (101) y, para totales y
hándicaps asiáticos a tiempo completo, únicamente la línea principal
(mainLine=True) de cada tipo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

MAIN_MARKET_TYPES = {"1x2", "totals", "spreads", "moneyline"}


@dataclass
class Snap:
    fixture_id: str
    bookmaker: str
    market_id: int
    market_type: str
    handicap: float | None
    outcome_id: int
    price: float
    changed_at: str | None
    age_s: int | None
    active: bool
    market_active: bool
    main_line: bool
    stake_limit: float | None
    back_size: float | None
    lay_price: float | None
    traded_volume: float | None


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def market_catalog_lookup(markets: dict[int, dict], mid: int) -> tuple[str, float | None, str]:
    m = markets.get(mid)
    if not m:
        return "unknown", None, "unknown"
    return m.get("market_type", "unknown"), m.get("handicap"), m.get("period", "")


def parse_fixture_odds(fixture: dict, markets: dict[int, dict], now: datetime | None = None,
                       keep_types: Iterable[str] = MAIN_MARKET_TYPES) -> list[Snap]:
    """Convierte un objeto fixture (con bookmakerOdds) en filas Snap."""
    now = now or datetime.now(timezone.utc)
    keep = set(keep_types)
    fid = fixture.get("fixtureId")
    out: list[Snap] = []
    for bk, bdata in (fixture.get("bookmakerOdds") or {}).items():
        mkts = bdata.get("markets") or {}
        for mid_s, m in mkts.items():
            try:
                mid = int(mid_s)
            except ValueError:
                continue
            mtype, hcap, period = market_catalog_lookup(markets, mid)
            if mtype not in keep or (period and period != "fulltime"):
                continue
            market_active = bool(m.get("marketActive", True))
            for oid_s, o in (m.get("outcomes") or {}).items():
                try:
                    oid = int(oid_s)
                except ValueError:
                    continue
                p = (o.get("players") or {}).get("0")
                if not p or p.get("price") in (None, 0):
                    continue
                main = bool(p.get("mainLine"))
                if mtype in ("totals", "spreads") and not main:
                    continue
                ts = parse_ts(p.get("changedAt"))
                age = int((now - ts).total_seconds()) if ts else None
                ex = p.get("exchangeMeta") or {}
                back = ex.get("availableToBack") or []
                lay = ex.get("availableToLay") or []
                out.append(Snap(
                    fixture_id=fid, bookmaker=bk, market_id=mid, market_type=mtype, handicap=hcap,
                    outcome_id=oid, price=float(p["price"]), changed_at=p.get("changedAt"), age_s=age,
                    active=bool(p.get("active", True)), market_active=market_active, main_line=main,
                    stake_limit=p.get("limit"),
                    back_size=(back[0].get("size") if back else None),
                    lay_price=(lay[0].get("price") if lay else None),
                    traded_volume=ex.get("tradedVolume"),
                ))
    return out


def parse_fixture_meta(f: dict) -> dict:
    return {
        "fixture_id": f.get("fixtureId"),
        "sport_id": f.get("sportId"),
        "tournament_id": f.get("tournamentId"),
        "tournament_name": f.get("tournamentName"),
        "start_time": f.get("startTime"),
        "home": f.get("participant1Name"),
        "away": f.get("participant2Name"),
        "status_id": f.get("statusId"),
        "updated_at": f.get("updatedAt"),
    }


def outcome_label(market_type: str, outcome_id: int, outcomes_json: list[dict] | None, handicap: float | None) -> str:
    name = None
    for o in outcomes_json or []:
        if int(o.get("outcomeId", -1)) == outcome_id:
            name = o.get("outcomeName")
            break
    name = name or str(outcome_id)
    if market_type == "totals" and handicap is not None:
        return f"{name} {handicap}"
    if market_type == "spreads" and handicap is not None:
        return f"{name} ({handicap:+})"
    return name
