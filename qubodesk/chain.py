"""web3 plumbing for QMS testnet (chain 19480) — also works against a local anvil."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
ABI = json.loads((ROOT / "abi/QuboMarket.json").read_text())
JOB_FIELDS = [c["name"] for c in next(f for f in ABI if f.get("name") == "getJob")["outputs"][0]["components"]]

QMS_RPC = "https://rpc.testnet.qms.finance"
QMS_CHAIN_ID = 19480
EXPLORER = "https://testnet.qmsscan.io"
LOG_CHUNK = 2000


class MarketNotConfigured(SystemExit):
    """Raised when .env has no MARKET_ADDRESS yet. A SystemExit so CLI users get a clean message."""


def load_env(path: str | None = None) -> None:
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Chain:
    def __init__(self, rpc: str | None = None, market: str | None = None, key: str | None = None):
        load_env()
        self.rpc = rpc or os.environ.get("QMS_RPC", QMS_RPC)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 30}))
        self.chain_id = self.w3.eth.chain_id
        addr = market or os.environ.get("MARKET_ADDRESS")
        if not addr or addr.startswith("0x..."):
            raise MarketNotConfigured("MARKET_ADDRESS not set (run ./scripts/deploy_qms.sh first)")
        self.market = self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI)
        self.deploy_block = int(os.environ.get("MARKET_DEPLOY_BLOCK", "0"))
        # QMS v0.1: `finalized` returns genesis → act on confirmation depth instead
        self.confirmations = int(os.environ.get("CONFIRMATIONS", "3"))
        pk = key or os.environ.get("PRIVATE_KEY")
        self.account = Account.from_key(pk) if pk else None

    @property
    def me(self) -> str:
        if not self.account:
            raise SystemExit("PRIVATE_KEY not set")
        return self.account.address

    def head(self) -> int:
        return self.w3.eth.block_number

    def safe_head(self) -> int:
        return max(0, self.head() - self.confirmations)

    # ───────────────────────────────────────────── tx
    def send(self, fn, value: int = 0, gas_mult: float = 1.25) -> dict:
        tx = fn.build_transaction(
            {
                "from": self.me,
                "value": value,
                "nonce": self.w3.eth.get_transaction_count(self.me, "pending"),
                "chainId": self.chain_id,
            }
        )
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * gas_mult)
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=300, poll_latency=2)
        if rcpt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return rcpt

    def wait_confirmations(self, rcpt: dict) -> None:
        while self.head() - rcpt.blockNumber < self.confirmations:
            time.sleep(2)

    # ───────────────────────────────────────────── reads
    def job(self, job_id: int) -> dict:
        return dict(zip(JOB_FIELDS, self.market.functions.getJob(job_id).call()))

    def commitment(self, job_id: int, who: str) -> tuple[bytes, int]:
        h, blk = self.market.functions.commitments(job_id, who).call()
        return h, blk

    def posted_events(self, from_block: int, to_block: int) -> list:
        out = []
        start = max(from_block, self.deploy_block)
        while start <= to_block:
            end = min(start + LOG_CHUNK - 1, to_block)
            out += self.market.events.JobPosted().get_logs(from_block=start, to_block=end)
            start = end + 1
        return out

    def posted_event(self, job_id: int):
        logs = self.market.events.JobPosted().get_logs(
            from_block=self.deploy_block, to_block="latest", argument_filters={"jobId": job_id}
        )
        if not logs:
            raise SystemExit(f"JobPosted for job {job_id} not found (set MARKET_DEPLOY_BLOCK?)")
        return logs[0]

    def tx_url(self, h) -> str:
        h = h.hex() if hasattr(h, "hex") else h
        h = h if h.startswith("0x") else "0x" + h
        return f"{EXPLORER}/tx/{h}" if self.chain_id == QMS_CHAIN_ID else h
