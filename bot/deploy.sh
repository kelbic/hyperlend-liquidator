#!/usr/bin/env bash
# Deploy HyperLendLiquidator.sol to HyperEVM (chainId 999) with the OPERATOR's key. This is the
# ONE live on-chain step the operator runs (the bot itself is deploy-ready but never deploys on
# its own). Requires: a funded key (a little HYPE for gas) and foundry.
#
# Usage:
#   HL_KEYFILE=~/.hyperlend-bot/key ./bot/deploy.sh        # key in a 600-perm file (preferred)
#   HL_PRIVATE_KEY=0x... ./bot/deploy.sh                   # or inline (avoid; shell history)
set -euo pipefail
cd "$(dirname "$0")/../contracts"

POOL=0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b          # HyperLend core Pool (verified on-chain)
RPC=${HL_RPC:-https://rpc.hyperliquid.xyz/evm}
KEYFILE=${HL_KEYFILE:-$HOME/.hyperlend-bot/key}

if [ -n "${HL_PRIVATE_KEY:-}" ]; then
    SIGN=(--private-key "$HL_PRIVATE_KEY")
    echo "signing: inline HL_PRIVATE_KEY"
elif [ -f "$KEYFILE" ]; then
    SIGN=(--private-key "$(cat "$KEYFILE")")
    echo "signing: key file $KEYFILE"
else
    echo "ERROR: no key. Set HL_PRIVATE_KEY or put a key in $KEYFILE (chmod 600)." >&2
    exit 1
fi

echo "Deploying HyperLendLiquidator(pool=$POOL) to HyperEVM via $RPC…"
# --constructor-args is variadic — keep it LAST so forge doesn't swallow later flags.
forge create src/HyperLendLiquidator.sol:HyperLendLiquidator \
    "${SIGN[@]}" \
    --rpc-url "$RPC" --broadcast \
    --constructor-args "$POOL"

echo
echo "Done. Copy 'Deployed to: 0x…' -> set HL_CONTRACT=0x… in ~/.hyperlend-bot/env, then:"
echo "  1) fund the deployer/owner wallet with a little HYPE for gas"
echo "  2) DRY_RUN=1 python3 -u -m bot.executor once   # verify it sees the book + candidates"
echo "  3) (optional) fork-test one real liquidation end-to-end (see README §go-live)"
echo "  4) flip DRY_RUN=0 in ~/.hyperlend-bot/env and start the service"
