"""Build a signed payout pause/resume tx (run on Aric's own offline machine).

    python ops/pause_tx.py pause  "Reason shown publicly" --nonce 1 > pause.json
    python ops/pause_tx.py renew  "Still investigating"   --nonce 2 > renew.json
    python ops/pause_tx.py resume "Fixed: ..."            --nonce 3 > resume.json

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
    ap.add_argument("--chain-id", default="netlithic-dev-1")
    a = ap.parse_args()
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
