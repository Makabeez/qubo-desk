"""Solver front-end: Rust `qsa` binary when built, numpy annealer otherwise."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from .qubo import Qubo

ROOT = Path(__file__).resolve().parent.parent
QSA_CANDIDATES = [os.environ.get("QSA_BIN", ""), str(ROOT / "solver/target/release/qsa"), shutil.which("qsa") or ""]


def qsa_path() -> str | None:
    for c in QSA_CANDIDATES:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def solve(
    q: Qubo, seconds: float = 5.0, sweeps: int = 1000, seed: int = 1, init: int | None = None, threads: int = 0
) -> dict:
    """Returns {"x": int, "energy": int, "engine": str, "ms": int}. Energy is recomputed exactly."""
    bin_ = qsa_path()
    if bin_:
        args = [bin_, "--restarts", "0", "--seconds", str(seconds), "--sweeps", str(sweeps), "--seed", str(seed)]
        if threads:
            args += ["--threads", str(threads)]
        if init is not None:
            args += ["--init", hex(init)]
        out = subprocess.run(args, input=q.to_solver_text(), capture_output=True, text=True, check=True)
        r = json.loads(out.stdout)
        x = int(r["x"], 16)
        return {"x": x, "energy": q.energy(x), "engine": "qsa", "ms": r["ms"], "restarts": r["restarts"]}
    return _numpy_sa(q, seconds, sweeps, seed, init)


def _numpy_sa(q: Qubo, seconds: float, sweeps: int, seed: int, init: int | None) -> dict:
    n = q.n
    Q = np.zeros((n, n))
    for (i, j), w in q.entries.items():
        if i == j:
            Q[i, i] += w
        else:
            Q[i, j] += w
            Q[j, i] += w
    diag = np.diag(Q).copy()
    off = Q - np.diag(diag)
    bound = np.abs(diag) + np.abs(off).sum(1)
    nz = np.abs(Q[Q != 0])
    b_hot, b_cold = math.log(2) / max(bound.max(), 1e-9), math.log(100) / max(nz.min(), 1e-9)
    rng = np.random.default_rng(seed)
    t0 = time.time()
    best_x, best_e, restarts = 0, q.energy(0), 0
    while True:
        x = (np.array([(init >> i) & 1 for i in range(n)]) if (init is not None and restarts == 0)
             else rng.integers(0, 2, n)).astype(float)
        h = diag + off @ x
        for s in range(sweeps):
            beta = b_hot * (b_cold / b_hot) ** (s / max(sweeps - 1, 1))
            for i in rng.permutation(n):
                d = h[i] if x[i] == 0 else -h[i]
                if d <= 0 or rng.random() < math.exp(-beta * d):
                    sgn = 1.0 if x[i] == 0 else -1.0
                    x[i] = 1 - x[i]
                    h += off[:, i] * sgn
        improved = True
        while improved:
            improved = False
            for i in range(n):
                d = h[i] if x[i] == 0 else -h[i]
                if d < 0:
                    sgn = 1.0 if x[i] == 0 else -1.0
                    x[i] = 1 - x[i]
                    h += off[:, i] * sgn
                    improved = True
        xi = sum(1 << i for i in range(n) if x[i] > 0.5)
        e = q.energy(xi)
        if e < best_e:
            best_x, best_e = xi, e
        restarts += 1
        if time.time() - t0 >= seconds:
            break
    return {"x": best_x, "energy": best_e, "engine": "numpy", "ms": int((time.time() - t0) * 1000), "restarts": restarts}
