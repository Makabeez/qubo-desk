"""Funding-carry portfolio  →  QUBO.

Each variable x_i is one delta-neutral basis leg on coin i (short perp / long spot when funding is
positive, the mirror when negative). We pick exactly k legs to

    maximise   carry  = Σ μ_i x_i
    minimise   risk   = Σ_ij Σ_ij x_i x_j          (covariance of the legs' funding streams)
    subject to Σ x_i = k

which becomes the unconstrained minimisation

    E(x) = -Σ μ_i x_i + λ xᵀΣx + A (Σ x_i - k)²

Expanding with x_i² = x_i (constant A·k² dropped):
    diag  Q_ii = -μ_i + λ Σ_ii + A (1 - 2k)
    upper Q_ij =  2 λ Σ_ij + 2A                  (i < j)

A is chosen just large enough that adding or removing one leg from any k-set can never pay off, so every
minimum is feasible. Coefficients are then scaled to int32 for the on-chain verifier.
"""
from __future__ import annotations

import csv
import json
import math
import time
import urllib.request
from dataclasses import dataclass

import numpy as np

from .qubo import Qubo, bits, mask

HOURS_PER_YEAR = 24 * 365


@dataclass
class Universe:
    names: list[str]
    sides: list[str]  # "short-perp" (collect +funding) | "long-perp" (collect -funding)
    mu: np.ndarray  # annualised net carry, fraction (0.12 = 12% APR)
    sigma: np.ndarray  # annualised covariance of carry streams

    @property
    def n(self) -> int:
        return len(self.names)


# ───────────────────────────────────────────────────────────── universe sources


def universe_from_funding_matrix(names, rates: np.ndarray, cost_apr: float = 0.0) -> Universe:
    """rates: T×N hourly funding rates (fractions). Direction per coin = sign of mean funding."""
    rates = np.asarray(rates, dtype=float)
    mean = np.nanmean(rates, axis=0)
    sign = np.where(mean >= 0, 1.0, -1.0)
    carry = np.nan_to_num(rates * sign) * HOURS_PER_YEAR  # annualised carry, one row per hour
    mu = carry.mean(axis=0) - cost_apr
    # Risk = how much each leg's carry swings day to day, and how the legs swing together.
    days = len(carry) // 24
    if days >= 3:
        daily = carry[: days * 24].reshape(days, 24, -1).mean(axis=1)
    else:
        daily = carry
    sigma = np.atleast_2d(np.cov(daily, rowvar=False))
    sides = ["short-perp" if s > 0 else "long-perp" for s in sign]
    return Universe(list(names), sides, mu, sigma)


def synthetic_universe(n: int = 24, hours: int = 24 * 30, seed: int = 7) -> Universe:
    """Deterministic fake market with clustered funding: 3 sectors that co-move."""
    rng = np.random.default_rng(seed)
    sectors = rng.integers(0, 3, size=n)
    base = rng.normal(1.2e-5, 1.0e-5, size=n)  # ~10% APR average
    # funding regimes persist: AR(1) sector factors (half-life ~1.5 days) + noisier idiosyncratic part
    phi = 0.98
    factor = np.zeros((hours, 3))
    for t in range(1, hours):
        factor[t] = phi * factor[t - 1] + rng.normal(0, 4e-6, size=3)
    loading = rng.uniform(0.3, 2.0, size=n) * (1 + 2 * (base > np.median(base)))  # rich carry = crowded
    idio = rng.normal(0, 1.0e-5, size=(hours, n))
    rates = base + factor[:, sectors] * loading + idio
    names = [f"SYN{i:02d}" for i in range(n)]
    return universe_from_funding_matrix(names, rates)


def universe_from_csv(path: str, cost_apr: float = 0.0) -> Universe:
    """Wide CSV: first column timestamp, one column per coin, hourly funding rates (fractions)."""
    with open(path) as f:
        rows = list(csv.reader(f))
    header, body = rows[0][1:], rows[1:]
    rates = np.array([[float(v) if v not in ("", "nan") else np.nan for v in r[1:]] for r in body])
    return universe_from_funding_matrix(header, rates, cost_apr)


def _hl_post(payload: dict, url: str = "https://api.hyperliquid.xyz/info"):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def universe_from_hyperliquid(top: int = 32, days: int = 14, cost_apr: float = 0.0) -> Universe:
    """Top-`top` perps by 24h notional volume, `days` of hourly funding history."""
    meta, ctxs = _hl_post({"type": "metaAndAssetCtxs"})
    assets = [
        (u["name"], float(c.get("dayNtlVlm") or 0))
        for u, c in zip(meta["universe"], ctxs)
        if not u.get("isDelisted")
    ]
    assets.sort(key=lambda a: -a[1])
    coins = [a[0] for a in assets[:top]]
    start = int((time.time() - days * 86400) * 1000)
    series = {}
    for coin in coins:
        hist = _hl_post({"type": "fundingHistory", "coin": coin, "startTime": start})
        series[coin] = {int(h["time"]) // 3_600_000: float(h["fundingRate"]) for h in hist}
        time.sleep(0.1)
    hours = sorted(set().union(*[s.keys() for s in series.values()]))
    rates = np.array([[series[c].get(h, np.nan) for c in coins] for h in hours])
    return universe_from_funding_matrix(coins, rates, cost_apr)


# ───────────────────────────────────────────────────────────── encoding


def encode(u: Universe, k: int, lam: float = 1.0, max_abs: int = 2**20) -> tuple[Qubo, dict]:
    n = u.n
    if not 1 <= k < n:
        raise ValueError("need 1 <= k < n")
    mu, S = u.mu, u.sigma
    # Largest gain any single flip could deliver, from the objective alone:
    flip_gain = np.abs(mu) + lam * (np.abs(np.diag(S)) + 2 * np.abs(S).sum(axis=1))
    A = 1.5 * float(flip_gain.max())

    coefs: dict[tuple[int, int], float] = {}
    for i in range(n):
        coefs[(i, i)] = -mu[i] + lam * S[i, i] + A * (1 - 2 * k)
        for j in range(i + 1, n):
            coefs[(i, j)] = 2 * lam * S[i, j] + 2 * A
    q, scale = Qubo.from_float_upper(n, coefs, max_abs=max_abs)
    meta = {
        "k": k,
        "lambda": lam,
        "penalty": A,
        "scale": scale,
        "names": u.names,
        "sides": u.sides,
        "mu": u.mu.tolist(),
        "sigma": u.sigma.tolist(),
    }
    return q, meta


def objective(meta: dict, x: int) -> dict:
    """Float view of a solution: carry, risk, objective, feasibility."""
    mu = np.array(meta["mu"])
    S = np.array(meta["sigma"])
    sel = bits(x, len(mu))
    v = np.zeros(len(mu))
    v[sel] = 1
    carry = float(mu @ v)
    risk = float(v @ S @ v)
    return {
        "legs": [{"coin": meta["names"][i], "side": meta["sides"][i], "carry_apr": mu[i]} for i in sel],
        "count": len(sel),
        "feasible": len(sel) == meta["k"],
        "carry_apr_sum": carry,
        "risk": risk,
        "objective": -carry + meta["lambda"] * risk,
        "sharpe_like": carry / math.sqrt(risk) if risk > 0 else float("inf"),
    }


def greedy_baseline(q: Qubo, meta: dict) -> int:
    """What a client gets without the market: top-k legs by carry."""
    order = np.argsort(-np.array(meta["mu"]))[: meta["k"]]
    return mask(int(i) for i in order)


def exact_minimum(q: Qubo) -> tuple[int, int]:
    """Brute force, n <= 22. For tests only."""
    if q.n > 22:
        raise ValueError("too large for brute force")
    best_x, best_e = 0, q.energy(0)
    for x in range(1, 1 << q.n):
        e = q.energy(x)
        if e < best_e:
            best_x, best_e = x, e
    return best_x, best_e
