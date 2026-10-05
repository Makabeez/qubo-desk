"""qubodesk — client CLI.

  encode   build a portfolio QUBO job file (synthetic | csv | hyperliquid)
  solve    solve a job file locally and print the book
  post     post a job file to QuboMarket and lock the fee
  jobs     list jobs on the market
  status   show one job
  settle   settle a job after its reveal window
  withdraw pull your credited balance
  book     decode a settled job's winning solution into legs (→ exec.py)
  bench    run the solver on a QMS block instance (.mtx.gz from testnet.qmsscan.io)
"""
from __future__ import annotations

import argparse
import json
import sys

from .encoder import (
    encode,
    exact_minimum,
    greedy_baseline,
    objective,
    synthetic_universe,
    universe_from_csv,
    universe_from_hyperliquid,
)
from .qubo import Qubo, dump

INT256_MAX = 2**255 - 1


def _load_job(path: str) -> tuple[Qubo, dict]:
    d = json.load(open(path))
    return Qubo.from_json(d["qubo"]), d["meta"]


def _print_book(meta: dict, x: int, energy: int | None = None) -> dict:
    o = objective(meta, x)
    print(f"  legs ({o['count']}/{meta['k']}{'' if o['feasible'] else '  ⚠ INFEASIBLE'}):")
    for leg in o["legs"]:
        print(f"    {leg['coin']:<10} {leg['side']:<11} carry {leg['carry_apr'] * 100:6.2f}% APR")
    print(f"  carry Σ {o['carry_apr_sum'] * 100:.2f}% APR | risk {o['risk']:.3e} | "
          f"carry/√risk {o['sharpe_like']:.2f}" + (f" | E={energy}" if energy is not None else ""))
    return o


# ───────────────────────────────────────────────────────────── commands


def cmd_encode(a):
    if a.source == "synthetic":
        u = synthetic_universe(a.n, seed=a.seed)
    elif a.source == "csv":
        u = universe_from_csv(a.csv, a.cost_apr)
    else:
        u = universe_from_hyperliquid(a.n, a.days, a.cost_apr)
    q, meta = encode(u, a.k, a.lam)
    base = greedy_baseline(q, meta)
    meta["baseline_x"] = hex(base)
    meta["baseline_energy"] = q.energy(base)
    meta["source"] = a.source
    dump(a.out, {"qubo": q.to_json(), "meta": meta})
    print(f"wrote {a.out}: n={q.n}, entries={len(q.entries)}, {len(q.pack())} bytes calldata, "
          f"k={a.k}, λ={a.lam}")
    print(f"greedy top-{a.k}-by-carry baseline E={meta['baseline_energy']}")
    _print_book(meta, base)


def cmd_solve(a):
    from .solver import solve

    q, meta = _load_job(a.job)
    r = solve(q, seconds=a.seconds)
    print(f"[{r['engine']}] E={r['energy']} in {r['ms']} ms ({r.get('restarts')} restarts)")
    print(f"baseline E={meta['baseline_energy']}  → improvement {meta['baseline_energy'] - r['energy']}")
    if a.exact:
        ex, ee = exact_minimum(q)
        print(f"exact optimum E={ee} ({'matched' if ee == r['energy'] else 'MISSED'})")
    _print_book(meta, r["x"], r["energy"])


def cmd_post(a):
    from web3 import Web3

    from .chain import Chain

    q, meta = _load_job(a.job)
    if a.accept == "any":
        accept = INT256_MAX
    elif a.accept == "baseline":
        accept = meta["baseline_energy"]  # pay for anything at least as good as greedy
    elif a.accept == "beat-baseline":
        accept = meta["baseline_energy"] - 1  # pay only for a strict improvement
    else:
        accept = int(a.accept)
    c = Chain()
    tag = Web3.keccak(text=json.dumps({k: meta[k] for k in ("names", "sides", "k", "lambda")}, sort_keys=True))
    fn = c.market.functions.postJob(q.pack(), q.n, a.commit_blocks, a.reveal_blocks, accept, tag)
    rcpt = c.send(fn, value=Web3.to_wei(a.fee, "ether"))
    ev = c.market.events.JobPosted().process_receipt(rcpt)[0].args
    print(f"job {ev.jobId} posted: fee {a.fee} QMS, commit ≤ #{ev.commitEnd}, reveal ≤ #{ev.revealEnd}, "
          f"accept E ≤ {'any' if accept == INT256_MAX else accept}")
    print(f"tx {c.tx_url(rcpt.transactionHash)}")


def cmd_jobs(a):
    from web3 import Web3

    from .chain import Chain

    c = Chain()
    n = c.market.functions.jobCount().call()
    head = c.head()
    print(f"{n} jobs, head #{head}")
    for jid in range(max(0, n - a.last), n):
        j = c.job(jid)
        phase = ("settled" if j["settled"] else "commit" if head <= j["commitEnd"]
                 else "reveal" if head <= j["revealEnd"] else "settleable")
        best = ("-" if j["bestSolver"] == "0x" + "0" * 40
                else f"{j['bestEnergy']} by {j['bestSolver']} (commit #{j['bestCommitBlock']})")
        print(f"  #{jid:<4} n={j['n']:<4} fee {Web3.from_wei(j['fee'], 'ether'):<8} {phase:<10} best {best}")


def cmd_status(a):
    from .chain import Chain

    c = Chain()
    j = c.job(a.id)
    j["qHash"] = "0x" + j["qHash"].hex()
    j["bestX"] = hex(j["bestX"])
    j["head"] = c.head()
    print(json.dumps(j, indent=2, default=str))


def cmd_settle(a):
    from .chain import Chain

    c = Chain()
    rcpt = c.send(c.market.functions.settle(a.id))
    ev = c.market.events.Settled().process_receipt(rcpt)[0].args
    print(f"job {a.id} settled → winner {ev.winner} E={ev.energy} payout {ev.payout}")
    print(f"tx {c.tx_url(rcpt.transactionHash)}")


def cmd_withdraw(a):
    from .chain import Chain

    c = Chain()
    rcpt = c.send(c.market.functions.withdraw())
    print(f"withdrawn: {c.tx_url(rcpt.transactionHash)}")


def cmd_book(a):
    from .chain import Chain

    q, meta = _load_job(a.job)
    c = Chain()
    j = c.job(a.id)
    ev = c.posted_event(a.id).args
    if bytes(ev.q) != q.pack():
        sys.exit("job file does not match the on-chain instance")
    if not j["settled"]:
        sys.exit("job not settled yet")
    x = j["bestX"]
    print(f"job {a.id}: winning E={j['bestEnergy']} (greedy baseline {meta['baseline_energy']})")
    o = _print_book(meta, x, j["bestEnergy"])
    book = {"job": a.id, "x": hex(x), "energy": j["bestEnergy"],
            "legs": [{"coin": l["coin"], "side": l["side"]} for l in o["legs"]]}
    dump(a.out, book)
    print(f"wrote {a.out} — feed it to `python -m qubodesk.exec {a.out}`")


def cmd_bench(a):
    import subprocess

    from . import mtx
    from .solver import qsa_path

    n, upper = mtx.read_mtx(a.file, a.convention)
    bin_ = qsa_path()
    if not bin_:
        sys.exit("bench needs the Rust solver: cargo build --release --manifest-path solver/Cargo.toml")
    out = subprocess.run([bin_, "--restarts", "0", "--seconds", str(a.seconds)],
                         input=mtx.to_solver_text(n, upper), capture_output=True, text=True, check=True)
    r = json.loads(out.stdout)
    x = int(r["x"], 16)
    e = mtx.energy(upper, x)
    print(f"n={n}, terms={len(upper)}, convention={a.convention}")
    print(f"qsa: E={e} in {r['ms']} ms ({r['restarts']} restarts)")
    if a.block_x is not None:
        bx = int(a.block_x, 16)
        be = mtx.energy(upper, bx)
        print(f"block winner (re-evaluated here): E={be}"
              + (f"  vs explorer {a.block_energy}" if a.block_energy is not None else ""))
        print(f"delta (ours - block) = {e - be}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qubodesk", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("encode")
    p.add_argument("--source", choices=["synthetic", "csv", "hl"], default="synthetic")
    p.add_argument("--csv", help="wide CSV of hourly funding (timestamp, COIN1, COIN2, ...)")
    p.add_argument("--n", type=int, default=24, help="universe size (synthetic / top-N for hl)")
    p.add_argument("--k", type=int, default=6, help="legs to hold")
    p.add_argument("--lam", type=float, default=1.0, help="risk aversion λ")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--cost-apr", type=float, default=0.0, help="fees/slippage to subtract, fraction APR")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default="job.json")
    p.set_defaults(fn=cmd_encode)

    p = sp.add_parser("solve")
    p.add_argument("job")
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--exact", action="store_true", help="also brute-force (n<=22)")
    p.set_defaults(fn=cmd_solve)

    p = sp.add_parser("post")
    p.add_argument("job")
    p.add_argument("--fee", default="0.01", help="QMS")
    p.add_argument("--commit-blocks", type=int, default=30)
    p.add_argument("--reveal-blocks", type=int, default=30)
    p.add_argument("--accept", default="baseline", help="any | baseline | beat-baseline | <int energy>")
    p.set_defaults(fn=cmd_post)

    p = sp.add_parser("jobs")
    p.add_argument("--last", type=int, default=20)
    p.set_defaults(fn=cmd_jobs)

    for name, fn in (("status", cmd_status), ("settle", cmd_settle)):
        p = sp.add_parser(name)
        p.add_argument("id", type=int)
        p.set_defaults(fn=fn)

    sp.add_parser("withdraw").set_defaults(fn=cmd_withdraw)

    p = sp.add_parser("book")
    p.add_argument("id", type=int)
    p.add_argument("job")
    p.add_argument("--out", default="book.json")
    p.set_defaults(fn=cmd_book)

    p = sp.add_parser("bench")
    p.add_argument("file", help=".mtx or .mtx.gz from a QMS block page")
    p.add_argument("--convention", choices=["full", "upper"], default="full")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--block-x", help="winning solution from the explorer, hex bitmask")
    p.add_argument("--block-energy", type=float, help="objective value shown on the explorer")
    p.set_defaults(fn=cmd_bench)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
