"""Netlithic command-line tool.

    python netlithic.py wallet new alice
    python netlithic.py node start 8000 --data-dir nodeA --peer http://127.0.0.1:8001
    python netlithic.py send <from> <to> 5
    python netlithic.py --help        (and `<command> --help`)
"""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request
from netutil import urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import economics  # noqa: E402
import node as node_mod  # noqa: E402
import netlithic_version  # noqa: E402
import wallet as wallet_mod  # noqa: E402

DEFAULT_NODE = f"http://127.0.0.1:{node_mod.DEFAULT_PORT}"


class CliError(Exception):
    pass


# ---------------------------------------------------------------- http helpers

def _request(url, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="GET" if data is None else "POST",
                  headers={"Content-Type": "application/json", "Accept": "application/json",
                           **(headers or {})})
    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        detail = body.get("reason") or body.get("error") or ""
        raise CliError(f"Node returned HTTP {e.code}: {detail}".rstrip(": "))
    except URLError as e:
        raise CliError(f"Could not reach node at {url}: {e.reason}")


def _node(args):
    return args.node.rstrip("/")


def _print(obj):
    print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------- wallets

def _wallets(args):
    wallet_mod.WALLET_FILE = args.wallets
    return wallet_mod.Wallet._load_wallets()


def _find_wallet(args, who):
    wallets = _wallets(args)
    matches = [w for a, w in wallets.items() if a == who.strip().lower() or w["name"] == who]
    if not matches:
        raise CliError(f"No wallet named or addressed {who!r} in {args.wallets}")
    if len(matches) > 1:
        raise CliError(f"Several wallets are named {who!r}; use the address instead")
    return matches[0]


def _public_view(w):
    return {"name": w["name"], "address": w["address"], "pubkey": w["pubkey"]}


def cmd_wallet_new(args):
    _wallets(args)  # sets WALLET_FILE
    w = wallet_mod.Wallet.create(args.name)
    print(f"Created wallet {w.name!r}")
    print(f"Address: {w.address}")
    print(f"Private key saved in {os.path.abspath(args.wallets)}. Keep that file private.")


def cmd_wallet_list(args):
    wallets = _wallets(args)
    if not wallets:
        print(f"(no wallets in {args.wallets}) - try: wallet new alice")
    for w in sorted(wallets.values(), key=lambda w: w["name"]):
        print(f"{w['name']}: {w['address']}")


def cmd_wallet_show(args):
    w = _find_wallet(args, args.wallet)
    view = _public_view(w)
    _print(view)
    if args.export_key:
        # Never printed: written to a new owner-only (0600) file instead.
        path = os.path.abspath(args.export_key)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CliError(f"{path} already exists; not overwriting")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"name": w["name"], "address": w["address"], "pubkey": w["pubkey"],
                       "private_key": w["private_key"]}, f, indent=2)
        print(f"Private key written to {path} (owner-only). Anyone with that file can spend "
              f"this wallet's funds; keep it offline.", file=sys.stderr)


# ---------------------------------------------------------------- node / chain

def cmd_node_start(args):
    node_mod.start(args)


def cmd_status(args):
    _print(_request(f"{_node(args)}/status"))


def cmd_points(args):
    data = _request(f"{_node(args)}/contributions")
    print(f"Contribution points at height {data['height']} "
          f"(+{data['rules']['per_block_mined']} per block mined, +{data['rules']['per_tx_sent']} per tx sent):")
    if not data["contributors"]:
        print("  (none yet)")
    for c in data["contributors"]:
        print(f"  {c['address']}  {c['cp']:>7} CP  ({c['blocks_mined']} blocks, "
              f"{c['empty_blocks']} empty, {c['tx_count']} txs)")


def cmd_economy(args):
    s = _request(f"{_node(args)}/status")
    print(f"Height {s['height']}. Supply minted {s['supply']['minted']} of {s['supply']['max_supply']} NLT.")
    print("Bucket      allocation     unlocked       locked         balance")
    for name, b in s["buckets"].items():
        print(f"{name:<11} {b['allocation']:>12}  {b['unlocked']:>12}  {b['locked']:>12}  {b['balance']:>14}")
    print(f"  (miners: {s['supply']['miner_rewards_paid']} paid as block rewards, "
          f"{s['supply']['miner_rewards_remaining']} left)")
    p = s["community_payouts"]
    print(f"Next community payout at height {p['next_epoch_height']} ({p['blocks_until']} blocks): "
          f"pool {p['pool_available_at_next_epoch']} NLT, cap {p['per_address_cap']} per address, "
          f"vesting over {p['vesting_blocks']} blocks.")
    if p["next_block_payouts"]:
        for x in p["next_block_payouts"]:
            print(f"  next block pays {x['amount']} NLT -> {x['recipient']}")
    print(f"Treasury/reserve spend approval: {p['treasury_reserve_spend_approval']}")
    ms = _request(f"{_node(args)}/multisig")
    if ms["mode"] == "multisig":
        print(f"  multisig {ms['threshold']}-of-{len(ms['keyholders'])}; next nonce "
              f"treasury {ms['next_nonce']['treasury']}, reserve {ms['next_nonce']['reserve']}")


def cmd_balance(args):
    _print(_request(f"{_node(args)}/balance?address={args.address}"))


def cmd_mine(args):
    miner = args.miner
    if not wallet_mod.ADDRESS_RE.fullmatch(miner.strip().lower()):
        miner = _find_wallet(args, miner)["address"]
    token = args.admin_token or os.environ.get("NETLITHIC_ADMIN_TOKEN", "")
    headers = {"X-Admin-Token": token} if token else None
    result = _request(f"{_node(args)}/mine", {"miner_address": miner}, headers)
    b = result["block"]
    reward = economics.to_nlt(b["transactions"][0]["amount"] + b["transactions"][0].get("fees", 0))
    txs = sum(1 for t in b["transactions"] if "payload" in t)
    payouts = sum(1 for t in b["transactions"] if t.get("type") == "payout")
    extra = f", {payouts} community payouts" if payouts else ""
    print(f"Mined block {b['index']} ({txs} txs, reward {reward} NLT{extra}) hash={b['hash'][:16]}...")


def cmd_send(args):
    w = _find_wallet(args, args.sender)
    sender = wallet_mod.Wallet(w["name"], w["private_key"], w["pubkey"])
    to = args.recipient
    if not wallet_mod.ADDRESS_RE.fullmatch(to.strip().lower()):
        to = _find_wallet(args, to)["address"]
    account = _request(f"{_node(args)}/account?address={sender.address}")
    amount = economics.parse_nlt(args.amount)
    fee = economics.parse_nlt(args.fee) if args.fee else None
    tx = sender.create_transaction(to_address=to, amount=amount, nonce=int(account["next_nonce"]), fee=fee)
    result = _request(f"{_node(args)}/submit_tx", {"tx": tx})
    print(f"Sent {economics.to_nlt(amount)} NLT from {sender.address} to {to} "
          f"(fee {economics.to_nlt(tx['payload']['fee'])} NLT, nonce {tx['payload']['nonce']})")
    _print(result)


def _read_spend(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise CliError(f"Could not read {path}: {e}")


def _write_spend(path, spend):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spend, f, indent=2)


def cmd_multisig_build(args):
    info = _request(f"{_node(args)}/multisig")
    if info["mode"] != "multisig":
        raise CliError(f"Node's approval mode is {info['mode']!r}, not multisig")
    import time
    to = args.recipient
    if not wallet_mod.ADDRESS_RE.fullmatch(to.strip().lower()):
        to = _find_wallet(args, to)["address"]
    payload = {
        "bucket": args.bucket,
        "recipient": to,
        "amount": economics.parse_nlt(args.amount),
        "nonce": int(info["next_nonce"][args.bucket]),
        "timestamp": time.time(),
        "chain_id": wallet_mod.current_chain_id(),
    }
    _write_spend(args.file, {"type": "multisig_spend", "payload": payload, "sigs": []})
    print(f"Wrote unsigned {args.bucket} spend of {args.amount} NLT to {to} (nonce {payload['nonce']}) "
          f"to {args.file}. Needs {info['threshold']} of {len(info['keyholders'])} keyholder signatures.")


def cmd_multisig_sign(args):
    spend = _read_spend(args.file)
    w = _find_wallet(args, args.wallet)
    signer = wallet_mod.Wallet(w["name"], w["private_key"], w["pubkey"])
    if any(s.get("pubkey") == signer.pubkey for s in spend.get("sigs", [])):
        raise CliError(f"{signer.name} already signed this spend")
    p = spend["payload"]
    print(f"Signing: {p['bucket']} pays {economics.to_nlt(p['amount'])} NLT to {p['recipient']} "
          f"(nonce {p['nonce']}, chain {p['chain_id']})")
    spend.setdefault("sigs", []).append(signer.sign_multisig(p))
    _write_spend(args.file, spend)
    print(f"Added signature from {signer.name} ({signer.pubkey[:16]}...). "
          f"{len(spend['sigs'])} signature(s) in {args.file}.")


def cmd_multisig_submit(args):
    spend = _read_spend(args.file)
    _print(_request(f"{_node(args)}/submit_tx", {"tx": spend}))


def _enter_data_dir(args):
    if not os.path.isdir(args.data_dir):
        raise CliError(f"No such data dir: {args.data_dir}")
    os.chdir(args.data_dir)


def cmd_validate(args):
    _enter_data_dir(args)
    if not os.path.exists("chain.json"):
        raise CliError(f"No chain.json in {os.getcwd()}")
    from core import Blockchain
    try:
        bc = Blockchain()
    except RuntimeError:
        print("INVALID: chain.json does not validate (old signatures, tampering or corruption).")
        return 1
    s = bc.supply_info()
    print(f"Valid chain. Height {len(bc.chain) - 1}, latest {bc.get_latest_block().hash[:16]}...")
    print(f"Supply: {s['minted']} of {s['max_supply']} NLT minted; genesis matches config.")
    return 0


def cmd_genesis(args):
    if not args.yes:
        raise CliError("This backs up and replaces chain.json, mempool.json and "
                       "state_contributions.json in the data dir. Re-run with --yes.")
    os.makedirs(args.data_dir, exist_ok=True)
    os.chdir(args.data_dir)
    import new_genesis
    return new_genesis.main(["--yes"])


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="netlithic", description="Netlithic command-line tool")
    p.add_argument("--version", action="version", version=netlithic_version.version_string())
    sub = p.add_subparsers(dest="command", required=True)

    def with_node(sp):
        sp.add_argument("--node", default=DEFAULT_NODE, help=f"node URL (default {DEFAULT_NODE})")
        return sp

    def with_wallets(sp):
        sp.add_argument("--wallets", default="wallets.json", help="wallet file (default ./wallets.json)")
        return sp

    w = sub.add_parser("wallet", help="create and inspect local wallets").add_subparsers(
        dest="wallet_command", required=True)
    sp = with_wallets(w.add_parser("new", help="create a new Ed25519 wallet"))
    sp.add_argument("name"); sp.set_defaults(func=cmd_wallet_new)
    with_wallets(w.add_parser("list", help="list wallet names and addresses")).set_defaults(func=cmd_wallet_list)
    sp = with_wallets(w.add_parser("show", help="show a wallet's public info (never the private key)"))
    sp.add_argument("wallet", help="wallet name or address")
    sp.add_argument("--export-key", metavar="FILE",
                    help="back up the private key to a NEW owner-only file (it is never printed)")
    sp.set_defaults(func=cmd_wallet_show)

    n = sub.add_parser("node", help="run a node").add_subparsers(dest="node_command", required=True)
    sp = n.add_parser("start", help="start a node")
    node_mod.add_node_args(sp)
    sp.set_defaults(func=cmd_node_start)

    with_node(sub.add_parser("status", help="show a node's status")).set_defaults(func=cmd_status)

    with_node(sub.add_parser("points", help="show contribution points (from the chain)")).set_defaults(func=cmd_points)
    with_node(sub.add_parser("economy", help="buckets (unlocked vs locked) and next community payout")).set_defaults(func=cmd_economy)

    sp = with_node(sub.add_parser("balance", help="show an address's balance"))
    sp.add_argument("address"); sp.set_defaults(func=cmd_balance)

    sp = with_wallets(with_node(sub.add_parser("send", help="sign and send a transfer")))
    sp.add_argument("sender", help="your wallet name or address")
    sp.add_argument("recipient", help="recipient address (or a local wallet name)")
    sp.add_argument("amount", help="amount in NLT, up to 8 decimals (e.g. 4 or 0.25)")
    sp.add_argument("--fee", help="fee in NLT (default: the network minimum, 0.0001)")
    sp.set_defaults(func=cmd_send)

    sp = with_wallets(with_node(sub.add_parser("mine", help="ask a node to mine one block (admin)")))
    sp.add_argument("miner", help="reward address (or a local wallet name)")
    sp.add_argument("--admin-token", default=None,
                    help="node admin token (default: $NETLITHIC_ADMIN_TOKEN). Prefer the env var; "
                         "command-line args can show up in process lists")
    sp.set_defaults(func=cmd_mine)

    ms = sub.add_parser("multisig", help="treasury/reserve M-of-N spends (build, co-sign, submit)"
                        ).add_subparsers(dest="multisig_command", required=True)
    sp = with_wallets(with_node(ms.add_parser("build", help="write an unsigned spend file")))
    sp.add_argument("bucket", choices=["treasury", "reserve"])
    sp.add_argument("recipient", help="recipient address (or a local wallet name)")
    sp.add_argument("amount", help="amount in NLT")
    sp.add_argument("--file", default="spend.json")
    sp.set_defaults(func=cmd_multisig_build)
    sp = with_wallets(ms.add_parser("sign", help="add your keyholder signature to a spend file"))
    sp.add_argument("wallet", help="your keyholder wallet name or address")
    sp.add_argument("--file", default="spend.json")
    sp.set_defaults(func=cmd_multisig_sign)
    sp = with_node(ms.add_parser("submit", help="send a fully signed spend file to a node"))
    sp.add_argument("--file", default="spend.json")
    sp.set_defaults(func=cmd_multisig_submit)

    sp = sub.add_parser("validate", help="check chain.json in a data dir")
    sp.add_argument("--data-dir", default="."); sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("genesis", help="back up and reset to a fresh genesis chain")
    sp.add_argument("--data-dir", default=".")
    sp.add_argument("--yes", action="store_true", help="confirm the reset")
    sp.set_defaults(func=cmd_genesis)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except CliError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())