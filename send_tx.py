import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request
from netutil import urlopen

from wallet import Wallet


NODE_URL = "http://127.0.0.1:8000"



def post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed


def get_json(url: str):
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def main():
    parser = argparse.ArgumentParser(description="Sign and submit a Netlithic transfer (Ed25519).")
    parser.add_argument("sender", help="sender address (must be in local wallets.json)")
    parser.add_argument("recipient", help="recipient address")
    parser.add_argument("amount", type=int)
    parser.add_argument("--node", default=NODE_URL)
    args = parser.parse_args()
    node_url = args.node.rstrip("/")

    sender_wallet = Wallet.load(args.sender)

    account = get_json(f"{node_url}/account?address={sender_wallet.address}")
    next_nonce = int(account["next_nonce"])

    print("Sender:", sender_wallet.address)
    print("Recipient:", args.recipient)
    print("Balance:", account["balance"])
    print("Next nonce:", next_nonce)

    tx = sender_wallet.create_transaction(
        to_address=args.recipient,
        amount=args.amount,
        nonce=next_nonce,
    )

    print("Generated tx:")
    print(json.dumps(tx, indent=2))

    status, result = post_json(f"{node_url}/submit_tx", {"tx": tx})

    print("HTTP status:", status)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()