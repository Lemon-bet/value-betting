"""Configuración del recolector de value betting.

Todo se lee de variables de entorno (fichero .env cargado por run.py) con
valores por defecto razonables para la fase de papel. Los parámetros de
detección reproducen la sección 4 del diseño v1; no cambiarlos según resultados.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


@dataclass
class Config:
    # --- API ---
    api_key: str = field(default_factory=lambda: _env("ODDSPAPI_KEY", ""))
    base_url: str = "https://api.oddspapi.io/v4"
    monthly_quota: int = field(default_factory=lambda: int(_env("VB_MONTHLY_QUOTA", "5000")))
    quota_reserve: int = field(default_factory=lambda: int(_env("VB_QUOTA_RESERVE", "300")))

    # --- Casas ---
    ref_bookmaker: str = field(default_factory=lambda: _env("VB_REF_BOOKMAKER", "pinnacle+30"))
    exchange_bookmaker: str = field(default_factory=lambda: _env("VB_EXCHANGE", "betfair-ex"))
    exchange_commission: float = field(default_factory=lambda: float(_env("VB_EXCHANGE_COMMISSION", "0.05")))
    soft_bookmakers: list[str] = field(
        default_factory=lambda: _env_list("VB_SOFT_BOOKMAKERS", "bet365.es,paf.es,leovegas.es,pokerstars.es")
    )

    # --- Competiciones (ids de OddsPapi; fútbol = sportId 10) ---
    sport_id: int = 10
    tournament_ids: list[int] = field(
        default_factory=lambda: [int(x) for x in _env_list("VB_TOURNAMENTS", "8")]
    )

    # --- Ventana de muestreo ---
    window_before_min: int = field(default_factory=lambda: int(_env("VB_WINDOW_BEFORE_MIN", "100")))
    window_after_min: int = field(default_factory=lambda: int(_env("VB_WINDOW_AFTER_MIN", "3")))
    sweep_interval_min: int = field(default_factory=lambda: int(_env("VB_SWEEP_INTERVAL_MIN", "10")))
    sweep_interval_max_min: int = field(default_factory=lambda: int(_env("VB_SWEEP_INTERVAL_MAX_MIN", "30")))
    fixtures_refresh_hours: int = field(default_factory=lambda: int(_env("VB_FIXTURES_REFRESH_H", "12")))
    fixtures_horizon_days: int = field(default_factory=lambda: int(_env("VB_FIXTURES_HORIZON_D", "8")))

    # --- Detección (diseño v1, sección 4) ---
    ev_min_2way: float = 0.025
    ev_min_1x2: float = 0.03
    ev_max: float = 0.12
    odds_min: float = 1.30
    odds_max_2way: float = 3.50
    odds_max_1x2: float = 4.50
    ref_max_age_s: int = field(default_factory=lambda: int(_env("VB_REF_MAX_AGE_S", "210")))  # 180 s + 30 s de retardo
    ref_move_threshold: float = 0.03  # Pinnacle movió >3 % desde el barrido anterior → esperar
    shin_power_max_diff: float = 0.005  # discrepancia máxima entre Shin y power (puntos de probabilidad)
    min_minutes_before_kickoff: int = 5
    signal_dedupe_min: int = 30  # no repetir la misma señal (partido, casa, mercado, resultado) en 30 min
    paper_stake_units: float = 1.0  # 1 % de la banca = 1 unidad

    # --- Mercados que se guardan y evalúan ---
    # 101 = 1X2; totales y hándicaps asiáticos: solo la línea principal (mainLine=True)
    market_1x2: int = 101
    market_types_2way: tuple[str, ...] = ("totals", "spreads")

    # --- Avisos ---
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))
    heartbeat_hours: int = field(default_factory=lambda: int(_env("VB_HEARTBEAT_H", "6")))

    # --- Rutas ---
    db_path: str = field(default_factory=lambda: _env("VB_DB", "data/vb.sqlite"))
    log_path: str = field(default_factory=lambda: _env("VB_LOG", "data/vb.log"))

    @property
    def all_bookmakers(self) -> list[str]:
        return [self.ref_bookmaker, self.exchange_bookmaker, *self.soft_bookmakers]

    def validate(self) -> None:
        if not self.api_key:
            raise SystemExit("Falta ODDSPAPI_KEY en el entorno o en .env")
        if not self.tournament_ids:
            raise SystemExit("VB_TOURNAMENTS vacío")


# Competiciones conocidas (OddsPapi, sportId 10). Se rellenan con `vb tournaments`.
KNOWN_TOURNAMENTS = {
    7: "UEFA Champions League",
    679: "UEFA Europa League",
    34480: "UEFA Conference League",
    8: "LaLiga",
    17: "Premier League",
    23: "Serie A",
    34: "Ligue 1",
    35: "Bundesliga",
    54: "LaLiga 2",
    329: "Copa del Rey",
}
