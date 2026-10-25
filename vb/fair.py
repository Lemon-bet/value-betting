"""Precio justo: métodos para quitar el margen de la casa de referencia.

Reglas fijas (diseño v1, sección 3.2):
- 2 resultados: multiplicativo.
- 3 resultados (1X2): Shin como principal, power como comprobación; si
  discrepan más de `max_diff` en alguna selección, la referencia se descarta.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


def implied(odds: list[float]) -> list[float]:
    return [1.0 / o for o in odds]


def margin(odds: list[float]) -> float:
    return sum(implied(odds)) - 1.0


def multiplicative(odds: list[float]) -> list[float]:
    p = implied(odds)
    s = sum(p)
    return [x / s for x in p]


def power(odds: list[float], tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """Encuentra k tal que sum(p_i^k) = 1, con p_i las probabilidades implícitas."""
    p = implied(odds)
    lo, hi = 0.5, 3.0
    for _ in range(max_iter):
        k = (lo + hi) / 2
        s = sum(x ** k for x in p)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return [x ** k for x in p]


def shin(odds: list[float], tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """Método de Shin (1993): z = proporción de apostantes informados.

    p_i = (sqrt(z^2 + 4(1-z) * pi_i^2 / B) - z) / (2(1-z)), con B = sum(pi_i).
    Se resuelve z por bisección para que sum(p_i) = 1.
    """
    pi = implied(odds)
    B = sum(pi)
    n = len(pi)

    def probs(z: float) -> list[float]:
        return [(sqrt(z * z + 4 * (1 - z) * x * x / B) - z) / (2 * (1 - z)) for x in pi]

    lo, hi = 0.0, 0.5
    for _ in range(max_iter):
        z = (lo + hi) / 2
        s = sum(probs(z))
        if abs(s - 1.0) < tol:
            break
        # sum decrece al aumentar z
        if s > 1.0:
            lo = z
        else:
            hi = z
    z = (lo + hi) / 2
    out = probs(z)
    s = sum(out)
    return [x / s for x in out] if n else out


@dataclass
class FairResult:
    probs: list[float]
    method: str
    margin: float
    ok: bool
    note: str = ""

    def fair_odds(self) -> list[float]:
        return [1.0 / p if p > 0 else float("inf") for p in self.probs]


def fair_probs(odds: list[float], max_diff: float = 0.005) -> FairResult:
    """Devuelve las probabilidades justas según el número de resultados."""
    if any(o is None or o <= 1.0 for o in odds):
        return FairResult([], "none", 0.0, False, "cuota inválida")
    m = margin(odds)
    if m < -0.01 or m > 0.15:
        return FairResult([], "none", m, False, f"margen fuera de rango ({m:.3f})")
    if len(odds) == 2:
        return FairResult(multiplicative(odds), "multiplicative", m, True)
    if len(odds) == 3:
        s = shin(odds)
        p = power(odds)
        diff = max(abs(a - b) for a, b in zip(s, p))
        if diff > max_diff:
            return FairResult(s, "shin", m, False, f"shin/power discrepan {diff:.4f}")
        return FairResult(s, "shin", m, True)
    return FairResult(multiplicative(odds), "multiplicative", m, True, "n>3, multiplicativo")


def net_price(price: float, commission: float) -> float:
    """Cuota neta de un exchange tras comisión sobre la ganancia."""
    return 1.0 + (price - 1.0) * (1.0 - commission)


def ev(fair_prob: float, price: float) -> float:
    return fair_prob * price - 1.0
