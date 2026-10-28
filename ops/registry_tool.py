#!/usr/bin/env python3
"""Registry tool: keep the TEMPORARY, CENTRALIZED registered-contributors list.

    python ops/registry_tool.py --check                      release gate: exit 1 while the REGISTRY_PUBKEY slot is empty
    python ops/registry_tool.py --status                     show the slot, always exit 0
    python ops/registry_tool.py set-key <PUBLIC key hex>     fill the slot in registry.py (public key only, never a private key)
    python ops/registry_tool.py verify LIST.json             check a list file (shape, addresses, size); no key needed
    python ops/registry_tool.py build LIST.json --nonce N [--registered CURRENT.json | --node URL] [--valid-until H] > unsigned.json
                                                             work out what must change and write UNSIGNED updates
                                                             (valid_until defaults to the node's next height + 2880 when --node is used)
    python ops/registry_tool.py rotate NEWKEY --nonce N [--node URL] [--valid-until H] > unsigned.json
                                                             write an UNSIGNED key rotation (the new key takes over 1,440 blocks later).
                                                             A rotation needs TWO signatures made offline: the CURRENT key signs
                                                             message_hex, the NEW key signs new_key_message_hex (so a typo or a key
                                                             nobody holds can never be installed)
    python ops/registry_tool.py assemble unsigned.json --signature HEX [--new-key-signature HEX] [--index 0] > tx.json
                                                             attach the signature(s) made elsewhere; check them; print the tx
                                                             (--new-key-signature is required for a rotation)
    python ops/registry_tool.py check-tx tx.json             verify a finished tx against the slot key

NO PRIVATE KEY EVER TOUCHES THIS TOOL OR THIS MACHINE. `build` writes the exact message to sign (message_hex).
The signature is made on the key holder's own offline machine (ops/registry_sign_offline.py, or any Ed25519 signer) and is
handed back as text. `assemble` only attaches and verifies it. Nothing here sends anything to a node: submitting the
finished tx is a separate, deliberate step (POST /submit_tx on a node), done by the founder.

Test coins have no value.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ops"))

import re  # noqa: E402

import registry  # noqa: E402
from set_pause_pubkey import key_id as _key_id, show as _show, validate as _validate  # noqa: E402

REGISTRY_PY = os.path.join(ROOT, "registry.py")
SLOT_RE = re.compile(r'^REGISTRY_PUBKEY = "([^"\n]*)"(  # REGISTRY-PUBKEY-SLOT.*)$', re.M)
ADDR_RE = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_CHAIN = "netlithic-dev-1"
SECRETISH = re.compile(r"(private|secret|seed|mnemonic|password)", re.I)


# ------------------------------------------------------------------ the key slot
def read_slot(path=REGISTRY_PY) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    found = SLOT_RE.findall(text)
    if len(found) != 1:
        raise SystemExit(f"Expected exactly one REGISTRY_PUBKEY slot line in {path}, found {len(found)}. Nothing changed.")
    return found[0][0]


def write_slot(path: str, key: str) -> None:
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    new, n = SLOT_RE.subn(lambda m: f'REGISTRY_PUBKEY = "{key}"{m.group(2)}', text)
    if n != 1:
        raise SystemExit("Expected exactly one slot line. Nothing changed.")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, path)


def pause_key(path=os.path.join(ROOT, "pause.py")) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^PAUSE_PUBKEY = "([^"\n]*)"', f.read(), re.M)
        return m.group(1).lower() if m else ""
    except OSError:
        return ""


# ------------------------------------------------------------------ list files
def load_list(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def verify_list(data: dict) -> list:
    """Return a list of plain-English problems (empty = fine)."""
    problems = []
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return ["The file must be a JSON object with an \"entries\" list."]
    seen = set()
    for i, e in enumerate(data["entries"], 1):
        where = f"entry {i}"
        if not isinstance(e, dict):
            problems.append(f"{where}: must be an object with an \"address\".")
            continue
        a = e.get("address")
        if not isinstance(a, str) or not ADDR_RE.match(a):
            problems.append(f"{where}: address must be 32 lowercase hex characters (a PUBLIC address).")
            continue
        if a in seen:
            problems.append(f"{where}: {a} appears twice.")
        seen.add(a)
        for k, v in e.items():
            if k != "address" and isinstance(v, str) and SECRETISH.search(k):
                problems.append(f"{where}: field \"{k}\" looks like a secret. Remove it; this file is public.")
    if len(seen) > registry.MAX_ACTIVE_ENTRIES:
        problems.append(f"{len(seen)} entries is more than the chain accepts ({registry.MAX_ACTIVE_ENTRIES}).")
    return problems


def addresses(data: dict) -> list:
    return sorted({e["address"] for e in data["entries"]})


def registered_from_info(info: dict) -> set:
    """Everybody the chain currently counts as listed (active or pending) from a /registry response."""
    return {x["address"] for x in info.get("active", [])} | {x["address"] for x in info.get("pending", [])}


# ------------------------------------------------------------------ build / assemble
VALID_FOR_BLOCKS = 2880        # default life of an update when the node's height is known (~2 days)


def build_updates(wanted: list, registered: set, nonce: int, chain_id: str, reason: str, pubkey: str = None,
                  valid_until: int = None) -> list:
    """Unsigned updates that turn `registered` into `wanted`. Respects the on-chain limits; refuses rather than splits
    past the per-period cap, because a bigger change must be spread over several days (that is the point of the cap)."""
    if valid_until is None:
        raise ValueError("Say until which block this update may be mined (--valid-until H, or use --node so the tool can work it out).")
    adds = sorted(set(wanted) - registered)
    removes = sorted(registered - set(wanted))
    if len(adds) > registry.MAX_ADDS_PER_WINDOW:
        raise ValueError(f"{len(adds)} additions are more than the chain allows in any {registry.REGISTRY_LIMIT_WINDOW} blocks "
                         f"({registry.MAX_ADDS_PER_WINDOW}). Add at most {registry.MAX_ADDS_PER_WINDOW} today, the rest tomorrow.")
    if len(removes) > registry.MAX_REMOVES_PER_WINDOW:
        raise ValueError(f"{len(removes)} removals are more than the chain allows in any {registry.REGISTRY_LIMIT_WINDOW} blocks "
                         f"({registry.MAX_REMOVES_PER_WINDOW}). Remove at most {registry.MAX_REMOVES_PER_WINDOW} today, the rest tomorrow.")
    if len(registered) + len(adds) - len(removes) > registry.MAX_ACTIVE_ENTRIES:
        raise ValueError("The list would be longer than the chain accepts.")
    batches, cur_a, cur_r = [], [], []
    for kind, items in (("r", removes), ("a", adds)):          # removals first: they are the urgent kind
        for x in items:
            if len(cur_a) + len(cur_r) >= registry.MAX_ENTRIES_PER_TX:
                batches.append((cur_a, cur_r))
                cur_a, cur_r = [], []
            (cur_a if kind == "a" else cur_r).append(x)
    if cur_a or cur_r:
        batches.append((cur_a, cur_r))
    out = []
    for i, (a, r) in enumerate(batches):
        reason_i = reason if len(batches) == 1 else f"{reason} ({i + 1}/{len(batches)})"
        payload = registry.registry_payload(a, r, reason_i, nonce + i, chain_id, valid_until)
        out.append({"kind": "registry_unsigned", "chain_id": chain_id, "nonce": nonce + i, "adds": payload["adds"],
                    "removes": payload["removes"], "reason": reason_i, "valid_until": valid_until,
                    "key_id": registry.key_id(pubkey) if pubkey else registry.key_id(),
                    "message_hex": registry.registry_message(payload).hex()})
    return out


def build_rotation(new_key: str, nonce: int, chain_id: str, reason: str, valid_until: int, pubkey: str = None) -> list:
    """One UNSIGNED rotation: the current registry key hands over to `new_key` 1,440 blocks after the block that carries it."""
    try:
        nk = _validate(new_key)
    except ValueError as e:
        raise ValueError(f"The new key is not a valid public key: {e}")
    if not registry.is_valid_pubkey_hex(nk):      # the very check consensus makes
        raise ValueError("The new key is not a valid public key: consensus would refuse it.")
    if nk == pause_key():
        raise ValueError("The new registry key must not be the pause key.")
    payload = registry.registry_payload([], [], reason, nonce, chain_id, valid_until, nk)
    return [{"kind": "registry_unsigned", "chain_id": chain_id, "nonce": nonce, "adds": [], "removes": [],
             "reason": reason, "valid_until": valid_until, "new_key": nk,
             "key_id": registry.key_id(pubkey) if pubkey else registry.key_id(),
             "new_key_id": registry.key_id(nk),
             "message_hex": registry.registry_message(payload).hex(),
             "new_key_message_hex": registry.registry_accept_message(payload).hex()}]


def assemble(unsigned: dict, signature: str, pubkey: str, new_key_signature: str = None) -> dict:
    sig = signature.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{128}", sig):
        raise ValueError("A signature is 128 hex characters.")
    if unsigned.get("kind") != "registry_unsigned":
        raise ValueError("That is not an unsigned registry update from `build`.")
    payload = registry.registry_payload(unsigned["adds"], unsigned["removes"], unsigned["reason"],
                                        unsigned["nonce"], unsigned["chain_id"], unsigned.get("valid_until"),
                                        unsigned.get("new_key"))
    if registry.registry_message(payload).hex() != unsigned["message_hex"]:
        raise ValueError("The message in the file does not match its contents: the file was edited after build.")
    tx = {"type": "registry", "adds": payload["adds"], "removes": payload["removes"], "reason": unsigned["reason"],
          "nonce": int(unsigned["nonce"]), "valid_until": int(unsigned["valid_until"]), "pubkey": pubkey, "sig": sig}
    if unsigned.get("new_key"):
        if registry.registry_accept_message(payload).hex() != unsigned.get("new_key_message_hex"):
            raise ValueError("The new-key message in the file does not match its contents: the file was edited after build.")
        nsig = (new_key_signature or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{128}", nsig):
            raise ValueError("A key rotation needs the NEW key's own signature too: --new-key-signature (128 hex characters, "
                             "made with ops/registry_sign_offline.py --as-new-key).")
        tx["new_key"] = unsigned["new_key"]
        tx["new_sig"] = nsig
    elif new_key_signature:
        raise ValueError("--new-key-signature only belongs to a key rotation.")
    if not registry.signature_ok(tx, unsigned["chain_id"], pubkey):
        raise ValueError("A signature does not verify for this update (the current registry key"
                         + (" and the new key must both sign" if unsigned.get("new_key") else "") + "). Nothing was made.")
    return tx


def _fetch(url: str) -> dict:
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("URL must be http or https")
    with urlopen(Request(url, headers={"User-Agent": "netlithic-registry-tool"}), timeout=30) as r:  # nosec B310 read-only GET
        return json.loads(r.read().decode())


# ------------------------------------------------------------------ commands
def cmd_status(check: bool, path: str = REGISTRY_PY) -> int:
    slot = read_slot(path)
    if not slot:
        print("REGISTRY_PUBKEY slot is EMPTY. With the registry on, nobody earns transfer points (safe), and no registry")
        print("update can be signed. Do NOT release this build to the public like this.")
        print("  Fix: python ops/registry_tool.py set-key <the founder's registry PUBLIC key>")
        print(f"  (or switch the registry off on purpose: registry.REGISTRY_ENABLED = False; enabled now: {registry.REGISTRY_ENABLED})")
        return 1 if check else 0
    try:
        key = _validate(slot)
    except ValueError as e:
        print(f"REGISTRY_PUBKEY slot holds an INVALID value: {e}")
        return 1 if check else 0
    if key == pause_key():
        print("REGISTRY_PUBKEY is the same as PAUSE_PUBKEY. Use a different key for each job.")
        return 1 if check else 0
    print("REGISTRY_PUBKEY slot is set:\n" + _show(key))
    return 0


def cmd_set_key(pub: str, force: bool, path: str) -> int:
    try:
        key = _validate(pub)
    except ValueError as e:
        print(f"Refused: {e}")
        return 2
    if key == pause_key():
        print("Refused: that is the pause key. The registry key must be a different key.")
        return 2
    current = read_slot(path)
    if current and current != key and not force:
        print("Refused: the slot already holds a different key. Use --force only if you mean to replace it (that is a fork).")
        return 2
    if current == key:
        print("Slot already holds this key. Nothing changed.\n" + _show(key))
        return 0
    write_slot(path, key)
    print(f"Wrote the PUBLIC key into {path}:\n" + _show(key))
    print("\nNext: read the key back to the founder and compare with the paper copy character by character. "
          "Nothing was pushed or deployed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="release gate: exit 1 while the key slot is empty or invalid")
    ap.add_argument("--status", action="store_true", help="show the key slot, exit 0")
    ap.add_argument("--file", default=REGISTRY_PY, help=argparse.SUPPRESS)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set-key", parents=[common]); p.add_argument("pubkey"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("verify", parents=[common]); p.add_argument("list")
    p = sub.add_parser("build", parents=[common]); p.add_argument("list"); p.add_argument("--nonce", type=int, required=True)
    p.add_argument("--registered", help="JSON saved from a node's /registry"); p.add_argument("--node")
    p.add_argument("--chain-id", default=DEFAULT_CHAIN); p.add_argument("--reason", default="Registry list update")
    p.add_argument("--pubkey", help=argparse.SUPPRESS); p.add_argument("--valid-until", type=int)
    p = sub.add_parser("rotate", parents=[common]); p.add_argument("newkey"); p.add_argument("--nonce", type=int, required=True)
    p.add_argument("--node"); p.add_argument("--chain-id", default=DEFAULT_CHAIN); p.add_argument("--valid-until", type=int)
    p.add_argument("--reason", default="Registry key rotation"); p.add_argument("--pubkey", help=argparse.SUPPRESS)
    p = sub.add_parser("assemble", parents=[common]); p.add_argument("unsigned"); p.add_argument("--signature", required=True)
    p.add_argument("--new-key-signature", dest="new_key_signature")
    p.add_argument("--index", type=int, default=0); p.add_argument("--pubkey", help=argparse.SUPPRESS)
    p = sub.add_parser("check-tx", parents=[common]); p.add_argument("tx"); p.add_argument("--chain-id", default=DEFAULT_CHAIN)
    a = ap.parse_args(argv)

    if a.check or a.status:
        return cmd_status(a.check, a.file)
    if a.cmd == "set-key":
        return cmd_set_key(a.pubkey, a.force, a.file)
    if a.cmd == "verify":
        problems = verify_list(load_list(a.list))
        for x in problems:
            print(" - " + x)
        if problems:
            return 1
        print(f"List is well formed: {len(addresses(load_list(a.list)))} address(es).")
        return 0
    if a.cmd == "build":
        data = load_list(a.list)
        problems = verify_list(data)
        if problems:
            print("\n".join(" - " + x for x in problems), file=sys.stderr)
            return 1
        if a.registered and a.node:
            ap.error("use --registered or --node, not both")
        if a.registered:
            info = load_list(a.registered)
        elif a.node:
            info = _fetch(a.node.rstrip("/") + "/registry")
        else:
            ap.error("say what the chain has now: --registered FILE (saved /registry) or --node URL")
        pub = a.pubkey or read_slot(a.file)
        vu = a.valid_until
        if vu is None and isinstance(info.get("next_height"), int):
            vu = info["next_height"] + VALID_FOR_BLOCKS
        try:
            updates = build_updates(addresses(data), registered_from_info(info), a.nonce, a.chain_id, a.reason, pub or None, vu)
        except ValueError as e:
            print(f"Refused: {e}", file=sys.stderr)
            return 1
        if not updates:
            print("The chain already matches this list. Nothing to sign.", file=sys.stderr)
        print(json.dumps(updates, indent=2))
        return 0
    if a.cmd == "rotate":
        vu = a.valid_until
        if vu is None and a.node:
            vu = _fetch(a.node.rstrip("/") + "/registry").get("next_height", 0) + VALID_FOR_BLOCKS
        try:
            if vu is None:
                raise ValueError("Say until which block this may be mined: --valid-until H (or --node URL).")
            print(json.dumps(build_rotation(a.newkey, a.nonce, a.chain_id, a.reason, vu, a.pubkey or read_slot(a.file) or None), indent=2))
        except ValueError as e:
            print(f"Refused: {e}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "assemble":
        pub = a.pubkey or read_slot(a.file)
        if not pub:
            print("Refused: the REGISTRY_PUBKEY slot is empty, so no signature can be checked.", file=sys.stderr)
            return 1
        with open(a.unsigned, encoding="utf-8") as f:
            items = json.load(f)
        items = items if isinstance(items, list) else [items]
        try:
            tx = assemble(items[a.index], a.signature, pub, a.new_key_signature)
        except (ValueError, IndexError, KeyError) as e:
            print(f"Refused: {e}", file=sys.stderr)
            return 1
        print(json.dumps(tx))
        return 0
    if a.cmd == "check-tx":
        pub = read_slot(a.file)
        if not pub:
            print("The REGISTRY_PUBKEY slot is empty: no registry tx can be valid.")
            return 1
        tx = load_list(a.tx)
        ok = registry.signature_ok(tx, a.chain_id, pub)
        print("Signature is valid for the registry key." if ok else "Signature is NOT valid for the registry key.")
        return 0 if ok else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
