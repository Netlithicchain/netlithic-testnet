"""Read-only release check for the points-v3 issuer list. No keys, no wallet files, no writes.

    python ops/check_family_issuers.py chain.json            # a saved /chain response (or a chain.json file)
    python ops/check_family_issuers.py http://host:8000      # fetches <url>/chain

Checks, in order (exit code 1 on the first two failing, 2 if only the warning applies):
  1. FAMILY_ISSUERS is non-empty, valid hex, no duplicates (the same test the node runs at startup).
  2. At least one listed issuer really sent >= 1 NLT to someone on this chain (else the family rule would
     silently switch itself off, which is the fail-safe, but means the list is wrong).
  3. Lists every address NOT in the list that sent >= 1 NLT to 3+ different addresses. Each one is either an
     ordinary hub or a faucet/pool/exchange wallet that should be added in a release BEFORE the rule is switched on.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contributions as c
from netutil import urlopen   # http/https only, no redirects


def load(src):
    if src.startswith(("http://", "https://")):
        with urlopen(src.rstrip("/") + "/chain", timeout=30) as r:
            data = json.load(r)
    else:
        data = json.load(open(src))
    return data["chain"] if isinstance(data, dict) and "chain" in data else data


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    chain = load(argv[1])
    try:
        c.validate_family_issuers()
    except c.FamilyIssuerError as e:
        print("FAIL:", e)
        return 1
    print(f"issuer list OK: {len(c.FAMILY_ISSUERS)} addresses; chain height {len(chain) - 1}")
    roots, seen = c._scan_funding(chain, c.MIN_POINT_TRANSFER, c.FAMILY_ISSUERS)
    senders = {}
    for b in chain:
        for tx in b.get("transactions") or []:
            p = tx.get("payload") if isinstance(tx, dict) else None
            if p and "type" not in tx and p.get("sender") in c.FAMILY_ISSUERS and int(p.get("amount", 0)) >= c.MIN_POINT_TRANSFER:
                senders.setdefault(p["sender"], set()).add(p["recipient"])
    for a in c.FAMILY_ISSUERS:
        print(f"  {a}: funded {len(senders.get(a, ()))} address(es) with >= 1 NLT")
    if not seen:
        print("FAIL: no listed issuer ever funded anyone on this chain; the family rule would be off (fail-safe).")
        return 1
    extra = c.unlisted_funders(chain)
    if extra:
        print("WARNING: not listed, but sent >= 1 NLT to 3+ addresses (hub or unlisted issuer?):")
        for a, n in sorted(extra.items(), key=lambda kv: -kv[1]):
            print(f"  {a}: {n} recipients")
        return 2
    print("no unlisted funders found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
