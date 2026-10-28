#!/usr/bin/env python3
"""Sign one unsigned registry update on the key holder's OWN OFFLINE machine. Never run this on a server or on the build box.

    python ops/registry_sign_offline.py unsigned.json [--index 0]      prints the signature (128 hex characters)
    python ops/registry_sign_offline.py unsigned.json --as-new-key     KEY ROTATION only: the NEW key signs "I accept this
                                                                       handover" (a second signature, made by whoever holds
                                                                       the new private key, on their own offline machine)

The private key is typed into a hidden prompt (never argv, env or a file on a server). The tool shows what you are about
to sign (addresses added/removed, reason, nonce) and asks for "yes". The signature is text: copy it back to the machine
that runs `ops/registry_tool.py assemble`. Any other Ed25519 signer works too (a hardware wallet, an air-gapped laptop):
it just has to sign the bytes of `message_hex`.
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("unsigned")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--as-new-key", action="store_true", help="sign as the NEW key of a rotation (co-signature)")
    a = ap.parse_args()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import registry
    with open(a.unsigned, encoding="utf-8") as f:
        items = json.load(f)
    u = (items if isinstance(items, list) else [items])[a.index]
    payload = registry.registry_payload(u["adds"], u["removes"], u["reason"], u["nonce"], u["chain_id"],
                                    u.get("valid_until"), u.get("new_key"))
    msg = registry.registry_message(payload)
    if msg.hex() != u["message_hex"]:
        sys.exit("The file was edited after it was built. Refusing to sign.")
    if a.as_new_key and not u.get("new_key"):
        sys.exit("This update is not a key rotation: there is no new key to sign for.")
    if a.as_new_key:
        msg = registry.registry_accept_message(payload)
        if msg.hex() != u.get("new_key_message_hex"):
            sys.exit("The file was edited after it was built. Refusing to sign.")
    print(f"chain      : {u['chain_id']}\nnonce      : {u['nonce']}\nreason     : {u['reason']}")
    print(f"valid until: block {u.get('valid_until')}")
    if u.get("new_key"):
        print(f"KEY ROTATION to new key {u['new_key']} (takes over 1,440 blocks after it is mined)")
    print(f"ADD    ({len(u['adds'])}): " + ", ".join(u["adds"]))
    print(f"REMOVE ({len(u['removes'])}): " + ", ".join(u["removes"]))
    if input("Sign this exactly? type yes: ").strip().lower() != "yes":
        sys.exit("Not signed.")
    if a.as_new_key:
        print("You are signing as the NEW key. Use the private key whose public key is the new_key above.")
    key = getpass.getpass(("NEW registry" if a.as_new_key else "Registry") + " private key (hex, hidden): ").strip()
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key))
    del key
    mine = sk.public_key().public_bytes_raw().hex()
    if a.as_new_key and mine != u.get("new_key"):
        sys.exit("That private key does not belong to the new_key in the file. Not signing.")
    if not a.as_new_key and u.get("key_id") and registry.key_id(mine) != u["key_id"]:
        sys.exit("That private key is not the registry key named in the file (key_id differs). Not signing.")
    print(sk.sign(msg).hex())


if __name__ == "__main__":
    main()
