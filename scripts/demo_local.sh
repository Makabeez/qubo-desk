#!/usr/bin/env bash
# End-to-end on a local anvil: deploy → encode → post → two competing solver bots → settle → book.
set -euo pipefail
cd "$(dirname "$0")/.."
# Foundry/Cargo FIRST on PATH: other packages ship a binary called `forge` (e.g. SNAP in /usr/bin)
export PATH="$HOME/.foundry/bin:$HOME/.cargo/bin:$PATH"
hash -r
forge --version 2>/dev/null | grep -qi "^forge" || {
  echo "Foundry not found (got: $(command -v forge || echo none)). Install: curl -L https://foundry.paradigm.xyz | bash && foundryup"; exit 1; }

# anvil's well-known dev keys (local only, never fund these anywhere)
CLIENT_PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
SOLVER1_PK=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
SOLVER2_PK=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a

anvil --block-time 1 --silent & ANVIL=$!
trap 'kill $ANVIL 2>/dev/null || true' EXIT
sleep 2

export QMS_RPC=http://127.0.0.1:8545 CONFIRMATIONS=1
MARKET=$(forge create src/QuboMarket.sol:QuboMarket --rpc-url $QMS_RPC --private-key $CLIENT_PK --broadcast \
  | awk '/Deployed to/{print $3}')
export MARKET_ADDRESS=$MARKET
echo "market: $MARKET"

python3 -m qubodesk encode --source synthetic --n 32 --k 8 --out demo-job.json
PRIVATE_KEY=$CLIENT_PK python3 -m qubodesk post demo-job.json --fee 1 --commit-blocks 12 --reveal-blocks 8 --accept beat-baseline

rm -f bot1.json bot2.json
# default: solver-1 thinks 3 s, solver-2 thinks 1 s → better energy wins
# TIE=1:   same budget, solver-1 starts 3 s late → equal energy, earliest COMMIT must win (solver-2)
if [ "${TIE:-0}" = "1" ]; then S1_SOLVE=3; S2_SOLVE=3; S1_DELAY=3; else S1_SOLVE=3; S2_SOLVE=1; S1_DELAY=0; fi
(sleep $S1_DELAY; PRIVATE_KEY=$SOLVER1_PK python3 -m qubodesk.bot --state bot1.json --block-time 1 --max-solve $S1_SOLVE --poll 1 --until-idle 2>&1 | sed 's/^/[solver-1] /') &
B1=$!
PRIVATE_KEY=$SOLVER2_PK python3 -m qubodesk.bot --state bot2.json --block-time 1 --max-solve $S2_SOLVE --poll 1 --until-idle 2>&1 | sed 's/^/[solver-2] /' &
B2=$!
wait $B1 $B2

PRIVATE_KEY=$CLIENT_PK python3 -m qubodesk jobs
PRIVATE_KEY=$CLIENT_PK python3 -m qubodesk book 0 demo-job.json --out demo-book.json
python3 -m qubodesk.exec demo-book.json --notional 1000
