"""Useful-work solver bot: watch QuboMarket, solve, commit, reveal, settle, withdraw.

State is persisted to a JSON file BEFORE every commit tx — losing a salt means the commitment can never be
revealed, so the file is the source of truth across restarts (run it under PM2).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from web3 import Web3

from .chain import Chain, MarketNotConfigured, load_env
from .qubo import Qubo
from .solver import solve

log = logging.getLogger("qubodesk.bot")


class Bot:
    def __init__(self, chain: Chain, state_path: str, min_fee_wei: int, max_solve_s: float,
                 block_time_s: float, margin_blocks: int):
        self.c = chain
        self.state_path = Path(state_path)
        self.min_fee = min_fee_wei
        self.max_solve = max_solve_s
        self.block_time = block_time_s
        self.margin = margin_blocks
        self.s = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.s.setdefault("scanned_to", max(self.c.deploy_block - 1, -1))
        self.s.setdefault("jobs", {})

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=2))
        os.replace(tmp, self.state_path)

    # ───────────────────────────────────────────── phases
    def discover(self) -> None:
        to = self.c.safe_head()
        frm = self.s["scanned_to"] + 1
        if frm > to:
            return
        for ev in self.c.posted_events(frm, to):
            a = ev.args
            jid = str(a.jobId)
            if jid in self.s["jobs"]:
                continue
            self.s["jobs"][jid] = {
                "status": "new",
                "n": a.n,
                "q": "0x" + bytes(a.q).hex(),
                "fee": a.fee,
                "commitEnd": a.commitEnd,
                "revealEnd": a.revealEnd,
                "accept": a.acceptEnergy,
            }
            log.info("job %s: n=%d fee=%s commitEnd=%d revealEnd=%d", jid, a.n,
                     Web3.from_wei(a.fee, "ether"), a.commitEnd, a.revealEnd)
        self.s["scanned_to"] = to
        self.save()

    def work(self, jid: str, j: dict, head: int) -> None:
        st = j["status"]

        if st == "new":
            if j["fee"] < self.min_fee:
                j["status"] = "skipped:fee"
                return
            blocks_left = j["commitEnd"] - head - self.margin
            if blocks_left <= 0:
                j["status"] = "skipped:late"
                return
            budget = min(self.max_solve, blocks_left * self.block_time * 0.5)
            q = Qubo.unpack(bytes.fromhex(j["q"][2:]), j["n"])
            r = solve(q, seconds=max(budget, 0.5))
            log.info("job %s: solved E=%d in %dms (%s, %s restarts)", jid, r["energy"], r["ms"], r["engine"],
                     r.get("restarts"))
            if r["energy"] > j["accept"]:
                j.update(status="skipped:below-bar", energy=r["energy"])
                log.info("job %s: best %d misses client's bar %d — not committing", jid, r["energy"], j["accept"])
                return
            salt = "0x" + secrets.token_bytes(32).hex()
            j.update(x=hex(r["x"]), energy=r["energy"], salt=salt, status="solved")
            self.save()  # persist the salt before the commitment exists on-chain
            st = "solved"

        if st == "solved":
            if head > j["commitEnd"]:
                j["status"] = "missed:commit"
                return
            c = self.c.market.functions.commitmentFor(int(jid), self.c.me, int(j["x"], 16), j["salt"]).call()
            rcpt = self.c.send(self.c.market.functions.commit(int(jid), c))
            j.update(status="committed", commitTx=rcpt.transactionHash.hex())
            log.info("job %s: committed at #%d %s", jid, rcpt.blockNumber, self.c.tx_url(rcpt.transactionHash))
            return

        if st == "committed" and head > j["commitEnd"]:
            if head > j["revealEnd"]:
                j["status"] = "missed:reveal"
                return
            h, my_block = self.c.commitment(int(jid), self.c.me)
            if h == b"\x00" * 32:
                j["status"] = "lost:commit-reorged"
                log.warning("job %s: commitment not on chain (reorg?)", jid)
                return
            job = self.c.job(int(jid))
            # same rule as the contract: lower energy, then earlier commit block
            beats = j["energy"] < job["bestEnergy"] or (
                j["energy"] == job["bestEnergy"] and my_block < job["bestCommitBlock"])
            if not beats:
                j["status"] = "outbid"  # revealing can't win; save the gas
                log.info("job %s: on-chain best E=%d (commit #%d) beats ours E=%d (commit #%d), not revealing",
                         jid, job["bestEnergy"], job["bestCommitBlock"], j["energy"], my_block)
                return
            q = bytes.fromhex(j["q"][2:])
            rcpt = self.c.send(self.c.market.functions.reveal(int(jid), int(j["x"], 16), j["salt"], q))
            j.update(status="revealed", revealTx=rcpt.transactionHash.hex(), commitBlock=my_block)
            log.info("job %s: revealed E=%d (commit #%d) %s", jid, j["energy"], my_block,
                     self.c.tx_url(rcpt.transactionHash))
            return

        if st in ("revealed", "outbid") and head > j["revealEnd"]:
            job = self.c.job(int(jid))
            won = job["bestSolver"] == self.c.me
            if won and not job["settled"]:
                rcpt = self.c.send(self.c.market.functions.settle(int(jid)))
                log.info("job %s: settled %s", jid, self.c.tx_url(rcpt.transactionHash))
            j["status"] = "won" if won else "lost"

    def withdraw_if_any(self) -> None:
        bal = self.c.market.functions.balances(self.c.me).call()
        if bal > 0:
            rcpt = self.c.send(self.c.market.functions.withdraw())
            log.info("withdrew %s QMS %s", Web3.from_wei(bal, "ether"), self.c.tx_url(rcpt.transactionHash))

    def tick(self) -> None:
        self.discover()
        head = self.c.head()
        for jid, j in sorted(self.s["jobs"].items(), key=lambda kv: int(kv[0])):
            if j["status"].split(":")[0] in ("skipped", "missed", "lost", "won"):
                continue
            try:
                self.work(jid, j, head)
            except Exception as e:  # keep the loop alive; the state machine retries next tick
                log.error("job %s: %s", jid, e)
            self.save()
        self.withdraw_if_any()

    def active(self) -> bool:
        return any(j["status"] in ("new", "solved", "committed", "revealed", "outbid")
                   for j in self.s["jobs"].values())


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="QuboDesk solver bot")
    ap.add_argument("--state", default="bot-state.json")
    ap.add_argument("--min-fee", default="0", help="minimum fee in QMS (ether units)")
    ap.add_argument("--max-solve", type=float, default=20.0, help="seconds per job")
    ap.add_argument("--block-time", type=float, default=10.0, help="QMS testnet target")
    ap.add_argument("--margin", type=int, default=2, help="blocks of slack before commitEnd")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--until-idle", action="store_true", help="exit once no job is in flight (demos/tests)")
    ap.add_argument("--env-file", help="per-solver key file (e.g. .env.solver1); overrides PRIVATE_KEY from "
                                       "the shell and from .env, shared settings still come from .env")
    a = ap.parse_args(argv)
    # stdout, so PM2 files normal activity under -out.log and only real crashes land in -error.log
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if a.env_file:
        load_env(a.env_file, override=True)
    # Under PM2 the bot may start before the market is deployed or while the RPC is down. Exiting would
    # make PM2 restart-loop it, so wait here and re-read .env until a market is reachable.
    while True:
        try:
            chain = Chain()
            break
        except MarketNotConfigured as e:
            log.warning("%s — retrying in 30 s", e)
        except Exception as e:
            log.warning("RPC not reachable (%s) — retrying in 30 s", e)
        time.sleep(30)
    bot = Bot(chain, a.state, Web3.to_wei(a.min_fee, "ether"), a.max_solve, a.block_time, a.margin)
    log.info("solver %s on chain %d, market %s", bot.c.me, bot.c.chain_id, bot.c.market.address)
    seen_job = False
    while True:
        bot.tick()
        seen_job |= bool(bot.s["jobs"])
        if a.until_idle and seen_job and not bot.active():
            log.info("idle — exiting")
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
