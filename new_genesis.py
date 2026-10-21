"""Start a fresh Netlithic chain (genesis block only) in the current folder.

Needed once after the Ed25519 upgrade: old chain.json files contain
pre-Ed25519 signatures and no longer validate. See MIGRATION.md.

Existing chain.json, mempool.json and state_contributions.json are NOT
deleted; they are renamed to <name>.pre-ed25519-<timestamp>.bak first.
wallets.json and peers.json are never touched.

Usage:  python new_genesis.py --yes
"""
import json
import os
import sys
import time

RESET_FILES = ["chain.json", "mempool.json", "state_contributions.json"]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--yes" not in argv:
        print(__doc__)
        print("Refusing to run without --yes.")
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name in RESET_FILES:
        if os.path.exists(name):
            backup = f"{name}.pre-ed25519-{stamp}.bak"
            os.replace(name, backup)
            print(f"Backed up {name} -> {backup}")

    with open("mempool.json", "w", encoding="utf-8") as f:
        json.dump([], f)
    with open("state_contributions.json", "w", encoding="utf-8") as f:
        json.dump({"users": {}}, f, indent=2)

    from core import Blockchain  # writes a fresh genesis chain.json
    from economics import to_nlt
    bc = Blockchain()
    print(f"Fresh chain created. Height {len(bc.chain) - 1}, genesis {bc.chain[0].hash[:16]}...")
    for tx in [t for t in bc.chain[0].transactions if t.get("type") == "allocation"]:
        print(f"  allocation {tx['bucket']:<9} {to_nlt(tx['amount']):>11} NLT -> {tx['recipient']} (locked)")
    print(f"  miners: {to_nlt(bc.econ.miner_bucket)} NLT, not pre-minted; paid as block rewards")
    if bc.econ.placeholders:
        print("NOTE: allocation addresses are PLACEHOLDERS from config/genesis_config.json. "
              "Set real addresses before any testnet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
