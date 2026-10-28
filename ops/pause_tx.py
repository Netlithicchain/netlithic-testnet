"""Build a signed payout pause/resume tx (run on the key holder's own offline machine).

    python ops/pause_tx.py pause  "Reason shown publicly" --nonce 1 --chain-id netlithic-dev-1 > pause.json
    python ops/pause_tx.py renew  "Still investigating"   --nonce 2 --chain-id netlithic-dev-1 > renew.json
    python ops/pause_tx.py resume "Fixed: ..."            --nonce 3 --chain-id netlithic-dev-1 > resume.json

--chain-id is REQUIRED (there is no default). A real pause uses --chain-id netlithic-dev-1. A practice run uses
--chain-id netlithic-drill: that tx is worthless on the real chain. A tx signed with the real chain id is a live
pause message that anyone who sees it could submit after block 5000, so never practise with the real id.

The private key is read from a hidden prompt (never from argv, env or a file
path on a server). Prints the tx JSON. Submit it to a node's /submit_tx:
    curl -s -X POST http://NODE:8000/submit_tx -H 'Content-Type: application/json' \
         -d "{\"tx\": $(cat pause.json)}"
Next nonce = /pause_status "nonce" + 1.
A pause expires by itself after pause.PAUSE_EXPIRY_BLOCKS (10080, ~7 days) blocks unless
renewed (a renew is a signed paused=true tx sent while the pause is active).
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pause  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["pause", "renew", "resume"])
    ap.add_argument("reason")
    ap.add_argument("--nonce", type=int, required=True)
    ap.add_argument("--chain-id", default=None)
    a = ap.parse_args()
    if not a.chain_id or not a.chain_id.strip():
        sys.exit("STOP: you must say which chain this is for. Add --chain-id netlithic-dev-1 for a REAL pause, or "
                 "--chain-id netlithic-drill for a practice run. Nothing was signed and no key was asked for.")
    a.chain_id = a.chain_id.strip()
    if not 1 <= len(a.reason) <= pause.MAX_REASON:
        sys.exit(f"reason must be 1..{pause.MAX_REASON} characters")
    key = getpass.getpass("Pause private key (hex, hidden): ").strip()
    tx = pause.make_pause_tx(key, a.action in ("pause", "renew"), a.reason, a.nonce, a.chain_id)
    del key
    if pause.PAUSE_PUBKEY and tx["pubkey"] != pause.PAUSE_PUBKEY:
        sys.exit("This key is not the configured PAUSE_PUBKEY; nodes would reject it.")
    print(json.dumps(tx))


if __name__ == "__main__":
    main()
