"""Burner solver wallets — create, fund from the main .env wallet, check balances.

    python -m qubodesk.wallets new solver1 --fund 3      # writes .env.solver1 (mode 600), sends 3 QMS
    python -m qubodesk.wallets fund solver1 --amount 2   # top up
    python -m qubodesk.wallets list                      # main wallet + every .env.<name>, with balances

Private keys are written to the file and NEVER printed. Each .env.<name> holds only PRIVATE_KEY; everything
else (RPC, market address, deploy block) is shared from .env. Testnet burners only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from eth_account import Account
from web3 import Web3

from .chain import EXPLORER, QMS_CHAIN_ID, QMS_RPC, ROOT, load_env, read_env

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _path(name: str) -> Path:
    if not NAME_RE.match(name) or name in ("example", "bak"):
        sys.exit(f"bad wallet name {name!r}: use lowercase letters, digits, - or _")
    return ROOT / f".env.{name}"


def _w3() -> Web3:
    load_env()
    return Web3(Web3.HTTPProvider(os.environ.get("QMS_RPC", QMS_RPC), request_kwargs={"timeout": 30}))


def _main_account():
    # always the key in .env itself — never whatever PRIVATE_KEY happens to be exported in the shell
    pk = read_env(ROOT / ".env").get("PRIVATE_KEY") if (ROOT / ".env").exists() else None
    if not pk:
        sys.exit("PRIVATE_KEY missing in .env — that wallet funds the solvers")
    return Account.from_key(pk)


def _addr_of(path: Path) -> str:
    pk = read_env(path).get("PRIVATE_KEY")
    if not pk:
        sys.exit(f"{path.name} has no PRIVATE_KEY")
    return Account.from_key(pk).address


def _url(kind: str, v: str, chain_id: int) -> str:
    return f"{EXPLORER}/{kind}/{v}" if chain_id == QMS_CHAIN_ID else v


def transfer(w3: Web3, sender, to: str, amount_qms: float) -> str:
    value = Web3.to_wei(amount_qms, "ether")
    if w3.eth.get_balance(sender.address) < value:
        sys.exit(f"funder {sender.address} has less than {amount_qms} QMS")
    base = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    tip = w3.eth.max_priority_fee
    tx = {
        "type": 2,
        "chainId": w3.eth.chain_id,
        "from": sender.address,
        "to": Web3.to_checksum_address(to),
        "value": value,
        "nonce": w3.eth.get_transaction_count(sender.address, "pending"),
        "gas": 21000,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": 2 * base + tip,
    }
    h = w3.eth.send_raw_transaction(sender.sign_transaction(tx).raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=300, poll_latency=2)
    if r.status != 1:
        sys.exit(f"transfer reverted: 0x{h.hex().removeprefix('0x')}")
    return "0x" + h.hex().removeprefix("0x")


def cmd_new(a) -> None:
    p = _path(a.name)
    if p.exists():
        sys.exit(f"{p.name} already exists ({_addr_of(p)}) — refusing to overwrite a key. Use `fund` to top up.")
    acct = Account.create()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# QuboDesk solver wallet '{a.name}' — testnet burner, never fund on mainnet\n")
        f.write(f"PRIVATE_KEY=0x{acct.key.hex().removeprefix('0x')}\n")
    print(f"created {p.name}: {acct.address}")
    if a.fund > 0:
        w3 = _w3()
        h = transfer(w3, _main_account(), acct.address, a.fund)
        print(f"funded {a.fund} QMS from main wallet: {_url('tx', h, w3.eth.chain_id)}")


def cmd_fund(a) -> None:
    p = _path(a.name)
    if not p.exists():
        sys.exit(f"{p.name} not found — create it with `new {a.name}`")
    w3 = _w3()
    to = _addr_of(p)
    h = transfer(w3, _main_account(), to, a.amount)
    print(f"sent {a.amount} QMS to {a.name} {to}: {_url('tx', h, w3.eth.chain_id)}")


def cmd_list(a) -> None:
    w3 = _w3()
    main = _main_account().address
    rows = [("main (.env)", main)]
    for p in sorted(ROOT.glob(".env.*")):
        if p.name in (".env.example", ".env.bak"):
            continue
        rows.append((p.name.removeprefix(".env."), _addr_of(p)))
    seen = {}
    for name, addr in rows:
        bal = Web3.from_wei(w3.eth.get_balance(addr), "ether")
        dup = f"   ⚠ SAME KEY AS {seen[addr]}" if addr in seen else ""
        seen.setdefault(addr, name)
        print(f"{name:<14} {addr}  {bal:.6f} QMS{dup}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="qubodesk.wallets", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("new")
    p.add_argument("name")
    p.add_argument("--fund", type=float, default=0.0, help="QMS to send from the main wallet")
    p.set_defaults(fn=cmd_new)
    p = sp.add_parser("fund")
    p.add_argument("name")
    p.add_argument("--amount", type=float, required=True)
    p.set_defaults(fn=cmd_fund)
    sp.add_parser("list").set_defaults(fn=cmd_list)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
