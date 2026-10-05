"""Matrix Market reader for the QUBO instances QMS publishes per block (testnet.qmsscan.io → .mtx.gz).

The exact objective convention of the QMS consensus solver isn't documented publicly yet, so both readings
are supported and `bench` reports which one reproduces the explorer's winning objective:
    full  : E(x) = xᵀ Q x       (an off-diagonal pair (i,j)/(j,i) counts twice in a symmetric file)
    upper : E(x) = Σ_{i<=j} Q_ij x_i x_j   (each stored off-diagonal entry counted once)
"""
from __future__ import annotations

import gzip
from collections import defaultdict


def read_mtx(path: str, convention: str = "full") -> tuple[int, dict[tuple[int, int], float]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        header = f.readline().lower().split()
        if not header or header[0] != "%%matrixmarket" or header[2] != "coordinate":
            raise ValueError("expected a coordinate Matrix Market file")
        symmetric = header[4] in ("symmetric", "skew-symmetric", "hermitian") if len(header) > 4 else False
        pattern = header[3] == "pattern"
        line = f.readline()
        while line.startswith("%"):
            line = f.readline()
        rows, cols, _nnz = map(int, line.split())
        n = max(rows, cols)
        upper: dict[tuple[int, int], float] = defaultdict(float)
        for line in f:
            if not line.strip() or line.startswith("%"):
                continue
            parts = line.split()
            i, j = int(parts[0]) - 1, int(parts[1]) - 1
            v = 1.0 if pattern else float(parts[2])
            a, b = min(i, j), max(i, j)
            if a == b:
                upper[(a, a)] += v
            elif convention == "upper":
                upper[(a, b)] += v
            else:  # full xᵀQx
                upper[(a, b)] += 2 * v if symmetric else v
    return n, dict(upper)


def to_solver_text(n: int, upper: dict[tuple[int, int], float]) -> str:
    items = [(i, j, w) for (i, j), w in sorted(upper.items()) if w != 0]
    return f"{n} {len(items)}\n" + "\n".join(f"{i} {j} {w!r}" for i, j, w in items) + "\n"


def energy(upper: dict[tuple[int, int], float], x: int) -> float:
    return sum(w for (i, j), w in upper.items() if (x >> i) & 1 and (x >> j) & 1)
