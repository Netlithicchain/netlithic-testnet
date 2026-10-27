"""Check a drill pause tx against the repo's PAUSE_PUBKEY, on chain "netlithic-drill".

    python3 verify_drill_tx.py tx.json                 # tx JSON from a file
    cat tx.json | python3 verify_drill_tx.py -         # or from stdin
    python3 verify_drill_tx.py tx.json --repo /path/to/netlithic-core --chain-id netlithic-drill

Uses the repo's own function pause.signed_by_pause_key(). Public data only: the tx holds a public key and a signature,
never a private key. Prints OK, FAIL or DANGER (exit code 0 / 1 / 2).

DANGER (exit 2): the tx is ALSO valid on the REAL chain netlithic-dev-1 (--real-chain-id). That happens when the signing
command was run without --chain-id netlithic-drill (older builds defaulted to the real chain). Such a tx is a live pause
message that anyone who saw it could submit after block 5000: do not photograph, copy or share it; tell the builder. Whitespace inside the hex fields (from copying a photo by
hand) is removed before checking; nothing else is "fixed".
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# inside a repo checkout (ops/verify_drill_tx.py) the repo is the parent folder; the loose copy uses the builder's checkout
_DEFAULT_REPO = os.path.dirname(_HERE) if os.path.isfile(os.path.join(os.path.dirname(_HERE), "pause.py")) else "/workspace/nl-v4h"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tx", help="file holding the tx JSON, or - for stdin")
    ap.add_argument("--repo", default=_DEFAULT_REPO, help="folder with pause.py (the 0.5.3 repo or the extracted drill zip)")
    ap.add_argument("--chain-id", default="netlithic-drill")
    ap.add_argument("--real-chain-id", default="netlithic-dev-1", help="the live chain; a tx valid here is DANGER")
    a = ap.parse_args()
    sys.path.insert(0, os.path.abspath(a.repo))
    import pause
    raw = sys.stdin.read() if a.tx == "-" else open(a.tx, encoding="utf-8").read()
    try:
        tx = json.loads(raw)
        if not isinstance(tx, dict):
            raise ValueError("not a JSON object")
    except ValueError as e:
        print(f"FAIL: could not read the tx as JSON ({e})")
        return 1
    for k in ("pubkey", "sig"):
        if isinstance(tx.get(k), str):
            tx[k] = "".join(tx[k].split())
    want = pause.PAUSE_PUBKEY
    print(f"repo PAUSE_PUBKEY : {want}  (key id {pause.key_id()})")
    print(f"tx pubkey         : {tx.get('pubkey')}")
    print(f"chain id checked  : {a.chain_id}")
    if not want:
        print("FAIL: the repo's PAUSE_PUBKEY slot is empty")
        return 1
    if str(tx.get("pubkey", "")).lower() != want.lower():
        print("FAIL: the tx was signed by a different key than the repo's PAUSE_PUBKEY")
        return 1
    signed = pause.signed_by_pause_key(tx, a.chain_id)
    real = pause.signed_by_pause_key(tx, a.real_chain_id)       # always asked, whatever happened above
    if real is not None:
        print("!" * 78)
        print(f"DANGER: this tx is VALID on the REAL chain {a.real_chain_id} (paused={real['paused']}, nonce={real['nonce']}).")
        print("It is a live pause message: anyone who sees it could submit it after block 5000.")
        print("Do NOT photograph, copy, send or save it anywhere. Tell the builder right away.")
        print("!" * 78)
        return 2
    if signed is None:
        print("FAIL: signature does not verify for this key, chain id and content")
        return 1
    print(f"OK: valid signature by the repo's pause key (paused={signed['paused']}, nonce={signed['nonce']}, reason={signed['reason']!r}); "
          f"not valid on the real chain {a.real_chain_id} (good)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
