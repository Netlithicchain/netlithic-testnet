"""One-command PAUSE DRILL on a private, in-memory test chain. Box only: no network, no real keys.

    python ops/drill/pause_drill.py

What it does (all in a throwaway temp directory, nothing is written to the repo):
  * runs the real ops/pause_keygen.py to make a THROWAWAY key (output captured, never printed or saved);
  * sets pause.PAUSE_PUBKEY to that throwaway PUBLIC key in this process only;
  * uses a different chain name (netlithic-dev-pause-drill) and forces the security fork on at height 0;
  * runs the real ops/pause_tx.py to sign pause / renew / resume, and checks that nodes accept them,
    that wrong key / wrong nonce / other-chain replay / tampering / empty reason are rejected, that epoch
    payouts are skipped while paused and come back after resume, and that a pause expires after
    pause.PAUSE_EXPIRY_BLOCKS;
  * shows why 0.5.0 (empty key) must not stay connected: the same pause block is REJECTED by a keyless node.

Prints PASS/FAIL per check and exits 1 on any FAIL. It cannot touch pause.py, chain.json or wallets.json
outside its own temp directory, and it refuses to run if the chain name does not start with "netlithic-dev".
"""
import builtins
import contextlib
import getpass
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

DRILL_CHAIN = "netlithic-dev-pause-drill"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    return ok


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def throwaway_key(keygen):
    """Run the real keygen tool with its output captured; return (private_hex, public_hex)."""
    answers = iter(["YES", ""])
    real_input, buf = builtins.input, io.StringIO()
    builtins.input = lambda prompt="": next(answers)
    try:
        with contextlib.redirect_stdout(buf):
            keygen.main()
    finally:
        builtins.input = real_input
    keys = re.findall(r"\b[0-9a-f]{64}\b", buf.getvalue())
    assert len(keys) == 2, "keygen output not understood"
    return keys[0], keys[1]


def sign(pause_tx, private_hex, action, reason, nonce, chain_id):
    """Run the real pause_tx tool (hidden prompt answered in-process) and return the tx dict."""
    real_gp, real_argv, buf = getpass.getpass, sys.argv, io.StringIO()
    getpass.getpass = lambda prompt="": private_hex
    sys.argv = ["pause_tx.py", action, reason, "--nonce", str(nonce), "--chain-id", chain_id]
    try:
        with contextlib.redirect_stdout(buf):
            pause_tx.main()
    finally:
        getpass.getpass, sys.argv = real_gp, real_argv
    return json.loads(buf.getvalue())


def main():
    global pause_tx
    with tempfile.TemporaryDirectory(prefix="pause-drill-") as tmp:
        os.chdir(tmp)
        import contributions as c
        import core
        import economics as eco
        import pause
        from conftest import build_block
        from wallet import Wallet

        assert DRILL_CHAIN.startswith(eco.DEV_CHAIN_PREFIX)
        keygen = _load("pause_keygen", "ops/pause_keygen.py")
        pause_tx = _load("pause_tx", "ops/pause_tx.py")

        # Private drill chain: fork on from height 0, short epochs, no address-age wait.
        core.SECURITY_FORK_HEIGHT = 0
        c.MIN_ADDRESS_AGE_BLOCKS = 0
        eco.TESTNET_SHORT_EPOCHS[DRILL_CHAIN] = (3, 4)
        econ = replace(eco.load_economics(), blocks_per_month=10, epoch_blocks=100, vesting_epochs=2,
                       chain_id=DRILL_CHAIN,
                       unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
        alice, bob = Wallet.create("alice"), Wallet.create("bob")
        bc = core.Blockchain(econ=econ)
        cid = bc.econ.chain_id
        check("drill chain name is private", cid == DRILL_CHAIN and cid != "netlithic-dev-1", cid)

        priv, pub = throwaway_key(keygen)
        other_priv, other_pub = throwaway_key(keygen)
        check("two different throwaway keys", pub != other_pub)

        def mine(addr, txs=()):
            b = bc.mine_block(addr, list(txs))
            ok = bc.add_block(b.to_dict())
            return ok

        # ---- A. 0.5.0 behaviour: empty key. The first pause block is invalid -> that node would fork off.
        pause.PAUSE_PUBKEY = pub  # pause_tx.py insists the key matches a configured key, if one is set
        tx0 = sign(pause_tx, priv, "pause", "drill: first pause", 1, cid)
        pause.PAUSE_PUBKEY = ""
        check("keyless node (0.5.0) rejects a pause tx", not bc.validate_candidate_tx_against_chain_and_mempool(tx0, []))
        bad = build_block(bc, [core.make_reward_tx(alice.address, bc.expected_reward(bc.chain)),
                               pause.normalize_pause_tx(tx0)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):   # the node logs the rejection; expected here
            rejected = not bc.add_block(bad.to_dict())
        check("keyless node (0.5.0) rejects the BLOCK that carries it (would fork)", rejected)
        check("/pause_status reports disabled when key empty", pause.state(bc.chain)["enabled"] is False)

        # ---- B. keyed node (0.5.1): the same tx is accepted.
        pause.PAUSE_PUBKEY = pub
        check("keyed node accepts the pause tx", bool(bc.validate_candidate_tx_against_chain_and_mempool(tx0, [])))
        check("only one pause tx at a time in the mempool", not bc.validate_candidate_tx_against_chain_and_mempool(tx0, [tx0]))
        wrong_key = pause.make_pause_tx(other_priv, True, "not the key holder", 1, cid)
        check("wrong key rejected", not bc.validate_candidate_tx_against_chain_and_mempool(wrong_key, []))
        check("wrong nonce rejected", not bc.validate_candidate_tx_against_chain_and_mempool(
            sign(pause_tx, priv, "pause", "skipped a nonce", 2, cid), []))
        check("other-chain replay rejected", not bc.validate_candidate_tx_against_chain_and_mempool(
            sign(pause_tx, priv, "pause", "signed for another chain", 1, "netlithic-dev-1"), []))
        check("tampered reason rejected", not bc.validate_candidate_tx_against_chain_and_mempool(dict(tx0, reason="changed"), []))
        try:
            pause.PAUSE_PUBKEY = other_pub
            sign(pause_tx, priv, "pause", "x", 1, cid)
            tool_guard = False
        except SystemExit:
            tool_guard = True
        finally:
            pause.PAUSE_PUBKEY = pub
        check("pause_tx.py refuses a key that is not the configured PAUSE_PUBKEY", tool_guard)

        # ---- C. full cycle: pause -> payouts skipped -> resume -> payouts back.
        check("block 1 mined", mine(alice.address))
        check("pause block accepted", mine(alice.address, [tx0]), bc.last_reject_reason)
        st = pause.state(bc.chain)
        check("state is paused, logged publicly", st["paused"] and st["log"][0]["reason"] == "drill: first pause" and st["enabled"])
        check("replaying the pause tx fails", not bc.validate_candidate_tx_against_chain_and_mempool(tx0, []))
        for n in (1, 2, 3):
            mine(bob.address, [alice.create_transaction("ab" * 16, eco.BASE_UNITS_PER_NLT, nonce=n, chain_id=cid)])
        renew = sign(pause_tx, priv, "renew", "drill: still investigating", 2, cid)
        check("renew accepted while paused", mine(bob.address, [renew]), bc.last_reject_reason)
        check("no payouts due while paused", bc.expected_payouts(bc.chain, 7) == [])
        mine(alice.address)
        check("payout epoch skipped while paused", not any(t.get("type") == "payout" for t in bc.chain[-1].transactions))
        check("log shows pause then renew", [e["action"] for e in pause.state(bc.chain)["log"]] == ["pause", "renew"])
        resume = sign(pause_tx, priv, "resume", "drill: fixed", 3, cid)
        check("resume accepted", mine(alice.address, [resume]), bc.last_reject_reason)
        check("state no longer paused", not pause.state(bc.chain)["paused"])
        for n in (4, 5):
            mine(bob.address, [alice.create_transaction("ab" * 16, eco.BASE_UNITS_PER_NLT, nonce=n, chain_id=cid)])
        check("payouts due again after resume", bool(bc.expected_payouts(bc.chain, 11)))
        check("whole drill chain validates", bc.is_valid_chain(bc.to_list()))

        # ---- D. auto-expiry (state() takes a height, so no need to mine 10,080 blocks).
        blocks = [b.to_dict() for b in bc.chain]
        blocks[-1]["transactions"] = list(blocks[-1]["transactions"]) + [
            {"type": "pause", "paused": True, "reason": "expiry probe", "nonce": 4}]
        at = blocks[-1]["index"]
        exp = pause.PAUSE_EXPIRY_BLOCKS
        check(f"pause still in force {exp} blocks after its last pause/renew", pause.state(blocks, height=at + exp)["paused"])
        check("pause lapses by itself one block later", not pause.state(blocks, height=at + exp + 1)["paused"])

        print()
        ok = all(RESULTS)
        print(f"PAUSE DRILL {'PASSED' if ok else 'FAILED'}: {sum(RESULTS)}/{len(RESULTS)} checks (chain {DRILL_CHAIN}, throwaway key, nothing saved)")
        return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    os.chdir(ROOT)
    sys.exit(code)
