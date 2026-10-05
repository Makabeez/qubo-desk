import gzip
import random

import numpy as np
import pytest

from qubodesk import mtx
from qubodesk.encoder import encode, exact_minimum, greedy_baseline, objective, synthetic_universe
from qubodesk.qubo import Qubo, bits
from qubodesk.solver import _numpy_sa, qsa_path, solve


def toy():
    q = Qubo(3)
    for i, j, w in [(0, 0, -5), (1, 1, -3), (2, 2, -4), (0, 1, 4), (0, 2, 6), (1, 2, 2)]:
        q.add(i, j, w)
    return q


def test_energy_and_wire_roundtrip():
    q = toy()
    assert q.energy(0b110) == -5 and q.energy(0b011) == -4 and q.energy(0b111) == 0
    packed = q.pack()
    assert len(packed) == 36
    assert packed[:6] == bytes([0, 0, 0xFF, 0xFF, 0xFF, 0xFB])  # (0,0,-5) big-endian int32
    q2 = Qubo.unpack(packed, 3)
    assert q2.entries == q.entries and q2.hash() == q.hash()


def test_add_normalises_lower_and_cancels():
    q = Qubo(4)
    q.add(3, 1, 7)
    q.add(1, 3, -7)
    assert q.entries == {}
    with pytest.raises(ValueError):
        q.add(0, 4, 1)


def test_int32_overflow_rejected():
    q = Qubo(2)
    q.add(0, 0, 2**31)
    with pytest.raises(ValueError):
        q.pack()


@pytest.mark.parametrize("seed", range(5))
def test_every_one_flip_local_min_is_feasible(seed):
    """The penalty A must make cardinality violations always repairable by a single improving flip."""
    u = synthetic_universe(12, seed=seed)
    q, meta = encode(u, k=4)
    for x in range(1 << q.n):
        e = q.energy(x)
        local_min = all(q.energy(x ^ (1 << i)) >= e for i in range(q.n))
        if local_min:
            assert len(bits(x, q.n)) == 4


@pytest.mark.parametrize("seed", range(4))
def test_solver_hits_exact_optimum_small(seed):
    u = synthetic_universe(16, seed=seed)
    q, meta = encode(u, k=5)
    _, e_star = exact_minimum(q)
    r = solve(q, seconds=0.5)
    assert r["energy"] == e_star
    assert objective(meta, r["x"])["feasible"]
    assert r["energy"] <= q.energy(greedy_baseline(q, meta))


def test_numpy_fallback_matches_on_toy():
    r = _numpy_sa(toy(), seconds=0.05, sweeps=50, seed=3, init=None)
    assert r["energy"] == -5


@pytest.mark.skipif(qsa_path() is None, reason="Rust solver not built")
def test_rust_random_dense_matches_bruteforce():
    rng = random.Random(1)
    for _ in range(5):
        q = Qubo(14)
        for i in range(14):
            for j in range(i, 14):
                q.add(i, j, rng.randint(-1000, 1000))
        _, e_star = exact_minimum(q)
        assert solve(q, seconds=0.3)["energy"] == e_star


def test_mtx_conventions(tmp_path):
    p = tmp_path / "inst.mtx.gz"
    with gzip.open(p, "wt") as f:
        f.write("%%MatrixMarket matrix coordinate real symmetric\n% qms block\n3 3 4\n1 1 -1\n2 2 -1\n2 1 3\n3 3 -2\n")
    n, full = mtx.read_mtx(str(p), "full")
    _, upper = mtx.read_mtx(str(p), "upper")
    assert n == 3
    assert full[(0, 1)] == 6 and upper[(0, 1)] == 3
    assert mtx.energy(full, 0b011) == -1 - 1 + 6
    text = mtx.to_solver_text(n, full)
    assert text.splitlines()[0] == "3 4"
