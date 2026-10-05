"""QUBO container + the exact wire format QuboMarket.sol verifies.

Wire format: packed upper-triangular entries, 6 bytes each
    [i:uint8][j:uint8][w:int32 big-endian]   with i <= j < n
    E(x) = sum w * x_i * x_j      (minimise; diagonal = linear terms)
Solutions are integers used as bitmasks: bit i = x_i.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Iterable

from eth_hash.auto import keccak

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
MAX_N = 256
MAX_ENTRIES = 8192


@dataclass
class Qubo:
    n: int
    entries: dict[tuple[int, int], int] = field(default_factory=dict)

    # ------------------------------------------------------------------ build
    def add(self, i: int, j: int, w: int) -> None:
        if i > j:
            i, j = j, i
        if not (0 <= i <= j < self.n):
            raise ValueError(f"index ({i},{j}) out of range for n={self.n}")
        v = self.entries.get((i, j), 0) + int(w)
        if v == 0:
            self.entries.pop((i, j), None)
        else:
            self.entries[(i, j)] = v

    @classmethod
    def from_float_upper(cls, n: int, coefs: dict[tuple[int, int], float], max_abs: int = 2**20):
        """Scale float coefficients to int32. Returns (qubo, scale) with int ≈ float * scale."""
        peak = max((abs(v) for v in coefs.values()), default=0.0)
        if peak == 0:
            raise ValueError("all-zero QUBO")
        scale = max_abs / peak
        q = cls(n)
        for (i, j), v in coefs.items():
            w = int(round(v * scale))
            if w:
                q.add(i, j, w)
        return q, scale

    # ------------------------------------------------------------------ eval
    def energy(self, x: int) -> int:
        e = 0
        for (i, j), w in self.entries.items():
            if (x >> i) & 1 and (x >> j) & 1:
                e += w
        return e

    # ------------------------------------------------------------------ wire
    def pack(self) -> bytes:
        self.check()
        out = bytearray()
        for (i, j), w in sorted(self.entries.items()):
            out += struct.pack(">BBi", i, j, w)
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes, n: int) -> "Qubo":
        if len(data) % 6:
            raise ValueError("packed QUBO length must be a multiple of 6")
        q = cls(n)
        for k in range(0, len(data), 6):
            i, j, w = struct.unpack(">BBi", data[k : k + 6])
            q.add(i, j, w)
        return q

    def hash(self) -> bytes:
        return keccak(self.pack())

    def check(self) -> None:
        if not (1 <= self.n <= MAX_N):
            raise ValueError(f"n must be 1..{MAX_N}")
        if not self.entries or len(self.entries) > MAX_ENTRIES:
            raise ValueError(f"entry count must be 1..{MAX_ENTRIES}")
        for (i, j), w in self.entries.items():
            if not (INT32_MIN <= w <= INT32_MAX):
                raise ValueError(f"weight {w} at ({i},{j}) overflows int32")

    # ------------------------------------------------------------------ io
    def to_solver_text(self) -> str:
        lines = [f"{self.n} {len(self.entries)}"]
        lines += [f"{i} {j} {w}" for (i, j), w in sorted(self.entries.items())]
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict:
        return {"n": self.n, "entries": [[i, j, w] for (i, j), w in sorted(self.entries.items())]}

    @classmethod
    def from_json(cls, d: dict) -> "Qubo":
        q = cls(int(d["n"]))
        for i, j, w in d["entries"]:
            q.add(int(i), int(j), int(w))
        return q


def bits(x: int, n: int) -> list[int]:
    return [i for i in range(n) if (x >> i) & 1]


def mask(indices: Iterable[int]) -> int:
    x = 0
    for i in indices:
        x |= 1 << i
    return x


def dump(path: str, obj: dict) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
