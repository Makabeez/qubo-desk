"""Turn a winning book (book.json from `qubodesk book`) into orders.

Default is a dry run that prints equal-notional orders. `--hl-testnet` places the PERP legs only on
Hyperliquid testnet via hyperliquid-python-sdk (pip install hyperliquid-python-sdk). The hedge leg
(spot / other venue) is out of scope here — on its own a perp leg is directional, so treat this as a
plumbing demo, never as a live strategy. Mainnet is deliberately not supported.

Env: HL_PRIVATE_KEY (agent/API wallet key), HL_ACCOUNT (main account address, if using an agent wallet).
"""
from __future__ import annotations

import argparse
import json
import os


def plan(book: dict, notional_usd: float, mids: dict[str, float] | None = None) -> list[dict]:
    per_leg = notional_usd / max(len(book["legs"]), 1)
    out = []
    for leg in book["legs"]:
        px = (mids or {}).get(leg["coin"])
        out.append({
            "coin": leg["coin"],
            "is_buy": leg["side"] == "long-perp",  # short-perp collects positive funding
            "usd": per_leg,
            "px": px,
            "sz": per_leg / px if px else None,
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book")
    ap.add_argument("--notional", type=float, default=1000.0, help="total USD across all legs")
    ap.add_argument("--hl-testnet", action="store_true", help="actually place perp orders on HL testnet")
    ap.add_argument("--slippage", type=float, default=0.01)
    a = ap.parse_args(argv)
    book = json.load(open(a.book))

    if not a.hl_testnet:
        for o in plan(book, a.notional):
            print(f"DRY  {'BUY ' if o['is_buy'] else 'SELL'} {o['coin']:<10} ${o['usd']:.2f}")
        return

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    wallet = Account.from_key(os.environ["HL_PRIVATE_KEY"])
    account = os.environ.get("HL_ACCOUNT", wallet.address)
    info = Info(constants.TESTNET_API_URL, skip_ws=True)
    ex = Exchange(wallet, constants.TESTNET_API_URL, account_address=account)
    decimals = {u["name"]: u["szDecimals"] for u in info.meta()["universe"]}
    mids = {k: float(v) for k, v in info.all_mids().items()}
    for o in plan(book, a.notional, mids):
        if o["coin"] not in decimals or not o["px"]:
            print(f"skip {o['coin']}: not listed on HL testnet")
            continue
        sz = round(o["sz"], decimals[o["coin"]])
        if sz <= 0:
            print(f"skip {o['coin']}: size rounds to zero")
            continue
        r = ex.market_open(o["coin"], o["is_buy"], sz, None, a.slippage)
        print(f"{'BUY ' if o['is_buy'] else 'SELL'} {o['coin']} {sz} → {r.get('status')} {r.get('response')}")


if __name__ == "__main__":
    main()
