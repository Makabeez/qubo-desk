#!/usr/bin/env bash
# Deploy + verify QuboMarket on QMS testnet (chain 19480), then record address + deploy block in .env.
# Works with a minimal .env containing only PRIVATE_KEY=... (Windows line endings tolerated).
set -euo pipefail
cd "$(dirname "$0")/.."
# Foundry/Cargo FIRST on PATH: other packages ship a binary called `forge` (e.g. SNAP in /usr/bin)
export PATH="$HOME/.foundry/bin:$HOME/.cargo/bin:$PATH"
hash -r
forge --version 2>/dev/null | grep -qi "^forge" || {
  echo "Foundry not found (got: $(command -v forge || echo none)). Install: curl -L https://foundry.paradigm.xyz | bash && foundryup"; exit 1; }
[ -f .env ] || { echo "no .env — cp .env.example .env and set PRIVATE_KEY (burner)"; exit 1; }

set -a; source <(tr -d '\r' < .env); set +a
: "${PRIVATE_KEY:?PRIVATE_KEY missing in .env}"
RPC=${QMS_RPC:-https://rpc.testnet.qms.finance}

# refuse to redeploy over a live market unless asked
if [ -n "${MARKET_ADDRESS:-}" ] && [ "${MARKET_ADDRESS}" != "0x..." ] && [ "${1:-}" != "--force" ]; then
  code=$(cast code "$MARKET_ADDRESS" --rpc-url "$RPC" 2>/dev/null || echo 0x)
  if [ "$code" != "0x" ]; then
    echo "MARKET_ADDRESS=$MARKET_ADDRESS already has code on chain. Re-run with --force to deploy a new one."
    exit 1
  fi
fi

CHAIN=$(cast chain-id --rpc-url "$RPC")
[ "$CHAIN" = "19480" ] || { echo "RPC $RPC is chain $CHAIN, not QMS testnet (19480)"; exit 1; }
ME=$(cast wallet address --private-key "$PRIVATE_KEY")
echo "deployer $ME — $(cast balance --ether "$ME" --rpc-url "$RPC") QMS"

OUT=$(forge create src/QuboMarket.sol:QuboMarket --rpc-url "$RPC" --private-key "$PRIVATE_KEY" --broadcast)
echo "$OUT"
ADDR=$(echo "$OUT" | awk '/Deployed to/{print $3}')
TX=$(echo "$OUT" | awk '/Transaction hash/{print $3}')
[ -n "$ADDR" ] || { echo "could not parse deployed address"; exit 1; }
BLOCK=$(cast receipt "$TX" blockNumber --rpc-url "$RPC")

setkv() {  # replace KEY=... if present, else append
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env
  else [ -n "$(tail -c1 .env)" ] && echo >> .env; echo "$1=$2" >> .env; fi
}
cp .env .env.bak
setkv MARKET_ADDRESS "$ADDR"
setkv MARKET_DEPLOY_BLOCK "$BLOCK"
grep -q '^CONFIRMATIONS=' .env || setkv CONFIRMATIONS 3
echo
echo "MARKET_ADDRESS=$ADDR  MARKET_DEPLOY_BLOCK=$BLOCK  → written to .env (backup .env.bak)"

echo "verifying on Blockscout…"
forge verify-contract --verifier blockscout --verifier-url https://testnet.qmsscan.io/api \
  --rpc-url "$RPC" "$ADDR" src/QuboMarket.sol:QuboMarket --watch \
  || echo "verification didn't go through yet — rerun: forge verify-contract --verifier blockscout --verifier-url https://testnet.qmsscan.io/api --rpc-url $RPC $ADDR src/QuboMarket.sol:QuboMarket --watch"
echo
echo "tx:       https://testnet.qmsscan.io/tx/$TX"
echo "contract: https://testnet.qmsscan.io/address/$ADDR"
