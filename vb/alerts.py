"""Avisos por Telegram (opcional: si no hay token, solo se registra en el log)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("vb.alerts")


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("[aviso] %s", text.replace("\n", " | "))
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=15,
            )
            if r.status_code != 200:
                log.warning("telegram %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.warning("telegram error: %s", e)
            return False


def format_signal(sig, fixture, signal_id: int, stake_units: float, is_exchange: bool) -> str:
    ko = fixture["start_time"][11:16] + " UTC" if fixture["start_time"] else "?"
    lines = [
        f"<b>VALOR {sig.ev:+.1%}</b> · {fixture['tournament_name'] or ''} · #{signal_id}",
        f"{fixture['home']} – {fixture['away']} ({ko}, en {sig.minutes_to_kickoff:.0f} min)",
        f"<b>{sig.bookmaker}</b> · {sig.market_type} · <b>{sig.outcome_label}</b> @ <b>{sig.price:.2f}</b>"
        + (f" (neto {sig.price_net:.2f})" if is_exchange else ""),
        f"Justa {sig.fair_odds:.2f} (ref {sig.ref_price:.2f} hace {sig.ref_age_s or 0}s · casa hace {sig.soft_age_s or 0}s)",
        f"Stake {stake_units:g} u",
    ]
    if sig.note:
        lines.append(f"⚠️ {sig.note}")
    return "\n".join(lines)
