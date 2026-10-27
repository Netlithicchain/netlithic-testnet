"""Security-review fixes (branch points-v4r-fixes). Every test here was written to FAIL on the reviewed build
(points-v4-registry b34d838) and pass now. Throwaway keys only; no network; test coins have no value.

  H1  mining pot: no empty-block discount; the pot follows block share (stated honestly); healthcheck clustering
  H2  registry: stolen key, rotation, rolling limits, prospective removal, valid_until, period-boundary adds
  M1  version gate: 0.5.3, a missing rules fingerprint is refused from the enforcement height
  M2  ops/verify_payouts.py: chain checks, paid-vs-recomputed, independent maths equals consensus maths
  M3  /payout_preview: cache, rate limit, snapshot (no state_lock), speed at 150,000 blocks
  LOW key_id never raises, reorg record persists, depth-3 consistency, the L1 docstring
"""
import copy
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request

import pytest

import contributions as c
import core
import economics as eco
import netlithic_version as nv
import pause
import registry
import test_registry as tr
from conftest import build_block, post, read_private_doc
from core import Block, Blockchain
from test_registry import (FORK, PERIOD, CID, MINER, HONEST, MERCH, KNOWN, addr, early_miner, key, pay_9320, rtx,
                           scan, second_period_traffic, synth, tx_from, use_key, chain_of, period_of)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))
import healthcheck as H  # noqa: E402
import verify_payouts as vp  # noqa: E402

_spec = importlib.util.spec_from_file_location("points_sim_fixes", os.path.join(ROOT, "ops", "sim", "points_sim.py"))
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

pytestmark = pytest.mark.registry
NLT = eco.BASE_UNITS_PER_NLT


@pytest.fixture
def bc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    return Blockchain()


@pytest.fixture
def tpl(wallets):
    alice, bob = wallets
    return alice.create_transaction(bob.address, NLT, nonce=1)


# =========================================================================================== H1

def _quiet_chain_with_dust_miner(bc, tpl, evil_addresses=50):
    """Period 1 [5000, 6440): 5% of the blocks go to ONE miner who spreads them over `evil_addresses` addresses and puts one
    1-base-unit self-transfer in each of them (below the 1 NLT minimum: no transfer points, but it made the block 'non-empty'
    under the old rule). The other 95% are 3 honest miners with EMPTY blocks (a quiet testnet)."""
    evil = [addr("evil", i) for i in range(evil_addresses)]
    honest = [addr("honest-miner", i) for i in range(3)]

    def miner_for(i):
        if i < FORK:
            return early_miner(i)
        return evil[(i // 20) % len(evil)] if i % 20 == 0 else honest[i % 3]

    def per_block(i):
        return [tx_from(tpl, addr("bank"), addr("bank2"), amount=1)] if FORK <= i < FORK + PERIOD and i % 20 == 0 else []

    synth(bc, 7879, per_block, miner_for)
    pays = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 7880)}
    budget = bc._v4_period_inputs(bc.chain, FORK, FORK + PERIOD)[1]
    mining_pot = budget - budget * c.TX_POT_PERCENT // 100
    return sum(pays.get(a, 0) for a in evil) / mining_pot, sum(pays.get(a, 0) for a in honest) / mining_pot


def test_h1_one_dust_transfer_per_block_no_longer_lets_a_five_percent_miner_take_the_pot(bc, tpl):
    evil_share, honest_share = _quiet_chain_with_dust_miner(bc, tpl)
    assert evil_share < 0.08                    # about its 5% block share (it was ~83% before the fix)
    assert honest_share > 0.05                  # the honest miners' empty blocks earn block points now


def test_h1_empty_and_non_empty_blocks_earn_the_same_block_points_from_the_fork_on():
    mk = lambda txs: {"index": FORK + 3, "transactions": [{"type": "reward", "recipient": "m", "amount": 0, "fees": 0}] + txs}
    dust = {"payload": {"sender": "x" * 32, "recipient": "y" * 32, "amount": 1, "nonce": 1, "fee": 10_000}}
    E = eco.load_economics()
    out = {}
    for label, blk in (("empty", mk([])), ("dust", mk([dust]))):
        out[label] = c.points_from_blocks([blk], E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                          E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK,
                                          first_seen={}, v3=True, flat_block_points_height=FORK)["m"]
    assert out["empty"]["blocks_mined"] - out["empty"]["empty_blocks"] == out["dust"]["blocks_mined"] - out["dust"]["empty_blocks"] == 1


def test_h1_history_before_the_fork_is_not_rewritten():
    E = eco.load_economics()
    blk = {"index": FORK - 5, "transactions": [{"type": "reward", "recipient": "m", "amount": 0, "fees": 0}]}
    u = c.points_from_blocks([blk], E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                             E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK, first_seen={}, v3=True,
                             flat_block_points_height=FORK)["m"]
    assert u["blocks_mined"] - u["empty_blocks"] == 0           # an empty block before 5000 still earns nothing


def test_h1_a_node_on_the_reviewed_rules_has_a_different_rules_fingerprint(monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)
    reviewed_build = "13a91549b79e6a23572197b05d162830bbc7636478e8c8d549d327953cc684ff"      # b34d838, registry off
    assert core.points_v3_rules_hash() != reviewed_build


@pytest.mark.parametrize("share", [5, 30, 60, 90])
def test_h1_the_mining_pot_follows_block_share_and_the_cap_does_not_stop_a_many_address_miner(share):
    one = sim.mining_run(share, 1)
    many = sim.mining_run(share, 100)
    assert one[2] <= 0.0201                                     # one address: held at 2% of the pot
    assert abs(many[2] - share / 100) < 0.08                    # many addresses: about the block share
    assert many[0] > one[0]


def test_h1_simulator_empty_block_case_old_rule_pays_the_trick_and_the_fix_does_not():
    _, res = sim.mining_tables()
    assert res[("quiet", "old (discount)", 50)]["pot_share"] > 0.7
    assert res[("quiet", "fixed (flat)", 50)]["pot_share"] < 0.08
    assert res[("quiet", "control", 50)]["miner"] == 0          # without the dust transfer the old rule paid it nothing
    text, _ = sim.mining_tables()
    assert "does not protect the mining pot" in text and "BLOCK SHARE" in text


def test_h1_docs_do_not_claim_the_split_protects_the_pool():
    doc = open(os.path.join(ROOT, "docs", "POINTS_V4.md")).read()
    assert "do **not** protect the mining pot" in doc
    assert "block share" in doc


# ---- healthcheck: cluster the miners by funding source
def _miner_chain(evil_addrs, funder, n_evil, n_other=100):
    chain = []
    for i in range(1, n_evil + n_other + 1):
        m = evil_addrs[i % len(evil_addrs)] if i <= n_evil * 2 and i % 2 == 0 and i // 2 <= n_evil else addr("o", i % 7)
        chain.append({"index": i, "transactions": [{"type": "reward", "recipient": m, "amount": 1, "fees": 0}]})
    base = len(chain)
    for k, a in enumerate(evil_addrs):
        chain.append({"index": base + 1 + k, "transactions": [
            {"type": "reward", "recipient": "z" * 32, "amount": 1, "fees": 0},
            {"payload": {"sender": funder, "recipient": a, "amount": 5, "nonce": k + 1, "fee": 1}}]})
    return chain


def test_h1_healthcheck_alerts_on_one_big_miner_and_on_a_cluster_funded_from_one_source():
    addrs = [addr("e", i) for i in range(20)]
    funder = addr("funder")
    miners = [addrs[i % 20] for i in range(60)] + [addr("o", i % 7) for i in range(40)]      # 60% over 20 addresses
    # no funding information: each address mined 3%, invisible (the honest limit, documented)
    assert H.miner_share_problems(miners) == {}
    funders = {a: funder for a in addrs}
    got = H.miner_share_problems(miners, funders=funders)
    assert list(got) == [f"miner_cluster:{funder}"] and "20 addresses" in got[f"miner_cluster:{funder}"]
    # a single address above the threshold
    got = H.miner_share_problems([addrs[0]] * 60 + [addr("o", i) for i in range(40)])
    assert list(got) == [f"miner_share:{addrs[0]}"]
    # the founder's own addresses can be ignored; short windows say nothing
    assert H.miner_share_problems([addrs[0]] * 60 + [addr("o", i) for i in range(40)], ignore=[addrs[0]]) == {}
    assert H.miner_share_problems([addrs[0]] * 10) == {}


def test_h1_healthcheck_reads_funding_from_the_chain_and_skips_chain_issued_coins():
    addrs = [addr("e", i) for i in range(5)]
    chain = _miner_chain(addrs, addr("funder"), 30)
    f = H.funding_sources(chain)
    assert all(f[a] == addr("funder") for a in addrs)
    assert H.funding_sources(chain, ignore=[addr("funder")]) == {}
    assert len(H.block_miners(chain)) == len(chain)
    assert H.cluster_root(addrs[0], {addrs[0]: "b", "b": addrs[0]}) in ("b", addrs[0])        # cycle-safe


# =========================================================================================== H2
def _state(k, monkeypatch):
    use_key(monkeypatch, k)
    return registry.RegistryState(period_of)


def test_h2_stolen_key_can_add_only_fifty_and_remove_only_fifty_per_rolling_day(monkeypatch):
    k = key()
    st = _state(k, monkeypatch)
    h = FORK + 100
    fakes = [addr("thief", i) for i in range(60)]
    assert st.check_and_apply(rtx(k, fakes[:50], nonce=1), h, CID)
    assert not st.check_and_apply(rtx(k, fakes[50:51], nonce=2), h + 1, CID)             # 51st in the day: refused
    honest = [addr("hon", i) for i in range(60)]
    k2 = k
    assert st.check_and_apply(rtx(k2, honest[:50], nonce=2), h + 1440, CID)             # next day: allowed
    assert st.check_and_apply(rtx(k2, [], honest[:50], nonce=3), h + 1441, CID)         # 50 removals
    assert not st.check_and_apply(rtx(k2, [], fakes[:1], nonce=4), h + 1442, CID)       # 51st removal inside the day: refused


def test_h2_period_boundary_adds_cannot_be_doubled(monkeypatch):
    """The old limit counted per payout period: 50 in the last block of one period + 50 in the first of the next = 100."""
    k = key()
    st = _state(k, monkeypatch)
    last_of_period = FORK + PERIOD - 1
    assert st.check_and_apply(rtx(k, [addr("a", i) for i in range(50)], nonce=1), last_of_period, CID)
    assert not st.check_and_apply(rtx(k, [addr("b", 0)], nonce=2), last_of_period + 1, CID)       # first block of next period
    assert not st.check_and_apply(rtx(k, [addr("b", 0)], nonce=2), last_of_period + 1439, CID)
    assert st.check_and_apply(rtx(k, [addr("b", 0)], nonce=2), last_of_period + 1440, CID)         # window has rolled off


def test_h2_removal_is_prospective_and_a_removal_does_not_touch_earned_blocks(monkeypatch):
    k = key()
    st = _state(k, monkeypatch)
    a = addr("hon", 0)
    assert st.check_and_apply(rtx(k, [a], nonce=1), FORK + 1, CID)
    assert st.check_and_apply(rtx(k, [], [a], nonce=2), FORK + 3000, CID)
    for h in (FORK + 1441, FORK + 2999, FORK + 3000, FORK + 3000 + 1439):
        assert st.eligible(a, h), h                              # earned while listed: still counts
    assert not st.eligible(a, FORK + 3000 + 1440)                # only transfers from a day after the removal stop


def test_h2_removal_after_the_period_end_leaves_the_payout_unchanged(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = addr("hon", 0)
    seq = {h: [tx_from(tpl, a, MERCH[0])] for h in range(6500, 7800, 7)}
    synth(bc, 9319, lambda i: seq.get(i, []), early_miner, reg={5001: [rtx(k, [a], nonce=1)]})
    before = pay_9320(bc)
    synth(bc, 9319, lambda i: seq.get(i, []), early_miner,
          reg={5001: [rtx(k, [a], nonce=1)], 8000: [rtx(k, [], [a], nonce=2)], 9300: [rtx(k, [], [], nonce=3)] if False else []})
    assert pay_9320(bc) == before and before.get(a, 0) > 0


def test_h2_valid_until_expiry_and_it_is_required(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = addr("x")
    h = FORK + 50
    expired = registry.sign_registry_tx(k, [a], [], "ok", 1, CID, valid_until=h - 1)
    assert not registry.RegistryState(period_of).check_and_apply(expired, h, CID)
    on_time = registry.sign_registry_tx(k, [a], [], "ok", 1, CID, valid_until=h)
    assert registry.RegistryState(period_of).check_and_apply(on_time, h, CID)
    missing = {kk: v for kk, v in on_time.items() if kk != "valid_until"}
    with pytest.raises(ValueError):
        registry.normalize_registry_tx(missing)
    assert not registry.RegistryState(period_of).check_and_apply(missing, h, CID)
    # a different chain id cannot reuse the signature
    assert not registry.RegistryState(period_of).check_and_apply(on_time, h, "netlithic-other")


def test_h2_key_rotation_takes_over_after_the_delay_and_the_old_key_stops_working(monkeypatch):
    old, new = key(), key()
    new_pub = registry.pubkey_from_private(new)
    st = _state(old, monkeypatch)
    h = FORK + 10
    rot = registry.sign_registry_tx(old, [], [], "rotate the key", 1, CID, valid_until=h + 100, new_key=new_pub,
                                  new_key_private=new)
    assert st.check_and_apply(rot, h, CID)
    assert st.pending_key(h + 1) == (h + registry.REGISTRY_ROTATION_DELAY_BLOCKS, new_pub)
    assert st.key_at(h + 1439) == registry.REGISTRY_PUBKEY and st.key_at(h + 1440) == new_pub
    # until the switch the OLD key still works, the new one does not
    assert not st.copy().check_and_apply(rtx(new, [addr("a")], nonce=2), h + 5, CID)
    assert st.copy().check_and_apply(rtx(old, [addr("a")], nonce=2), h + 5, CID)
    # after the switch: only the new key
    assert st.copy().check_and_apply(rtx(new, [addr("a")], nonce=2), h + 1440, CID)
    assert not st.copy().check_and_apply(rtx(old, [addr("a")], nonce=2), h + 1440, CID)


def test_h2_rotation_can_be_replaced_while_pending_and_must_use_a_new_key(monkeypatch):
    old, new1, new2 = key(), key(), key()
    st = _state(old, monkeypatch)
    h = FORK + 10
    same = registry.sign_registry_tx(old, [], [], "same", 1, CID, valid_until=h + 9, new_key=registry.REGISTRY_PUBKEY)
    assert not st.check_and_apply(same, h, CID)
    pause_key_hex = key()
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", registry.pubkey_from_private(pause_key_hex))
    to_pause = registry.sign_registry_tx(old, [], [], "p", 1, CID, valid_until=h + 9, new_key=pause.PAUSE_PUBKEY)
    assert not st.check_and_apply(to_pause, h, CID)                      # one key, one job
    r1 = registry.sign_registry_tx(old, [], [], "r1", 1, CID, valid_until=h + 9, new_key=registry.pubkey_from_private(new1),
                                  new_key_private=new1)
    assert st.check_and_apply(r1, h, CID)
    r2 = registry.sign_registry_tx(old, [], [], "r2", 2, CID, valid_until=h + 99, new_key=registry.pubkey_from_private(new2),
                                  new_key_private=new2)
    assert st.check_and_apply(r2, h + 50, CID)                           # the still-current key replaces the pending one
    assert st.key_at(h + 50 + 1440) == registry.pubkey_from_private(new2)
    assert st.key_at(h + 1440) == registry.REGISTRY_PUBKEY               # the first rotation never took over


def test_h2_a_rotation_is_a_stand_alone_update(monkeypatch):
    old, new = key(), key()
    st = _state(old, monkeypatch)
    mixed = registry.sign_registry_tx(old, [addr("a")], [], "mixed", 1, CID, valid_until=FORK + 99,
                                      new_key=registry.pubkey_from_private(new), new_key_private=new)
    assert not st.check_and_apply(mixed, FORK + 10, CID)               # a rotation carries no add/remove list
    bad_new = registry.sign_registry_tx(old, [], [], "bad", 1, CID, valid_until=FORK + 99, new_key="zz")
    assert not registry.RegistryState(period_of).check_and_apply(bad_new, FORK + 10, CID)


def test_h2_a_thief_holding_the_current_key_cannot_be_locked_out_by_rotation_alone(monkeypatch):
    """Documented limit: the chain has no second authority. The thief re-rotates before the owner's key takes over."""
    owner, thief_key = key(), key()                 # (the thief has the same private key as the owner in reality)
    st = _state(owner, monkeypatch)
    h = FORK + 10
    owner_new, thief_new = key(), key()
    assert st.check_and_apply(registry.sign_registry_tx(owner, [], [], "owner", 1, CID, valid_until=h + 9,
                                                        new_key=registry.pubkey_from_private(owner_new),
                                                        new_key_private=owner_new), h, CID)
    assert st.check_and_apply(registry.sign_registry_tx(owner, [], [], "thief", 2, CID, valid_until=h + 99,
                                                        new_key=registry.pubkey_from_private(thief_new),
                                                        new_key_private=thief_new), h + 60, CID)
    assert st.key_at(h + 60 + 1440) == registry.pubkey_from_private(thief_new)
    doc = open(os.path.join(ROOT, "docs", "POINTS_V4_REGISTRY.md")).read()
    assert "break-glass" in doc and "hard fork" in doc


def test_h2_key_id_never_raises_on_a_malformed_slot(monkeypatch):
    for bad in ("zz", "abc", "x" * 64, 123):
        assert registry.key_id(bad) == "invalid"
    monkeypatch.setattr(registry, "REGISTRY_PUBKEY", "not hex at all")
    assert registry.key_id() == "invalid"
    assert registry.key_id("") == ""


def test_h2_registry_tool_sets_valid_until_and_has_a_rotate_command():
    import registry_tool
    assert registry_tool.VALID_FOR_BLOCKS == 2880
    assert hasattr(registry_tool, "main")


# =========================================================================================== M1
def test_m1_version_and_gate_constants():
    assert nv.VERSION == nv.MIN_PEER_VERSION == "0.5.3"
    assert nv.RULES_FIELD_REQUIRED_HEIGHT == nv.UPGRADE_DEADLINE_HEIGHT == 4900
    assert not nv.rules_field_required(4899) and nv.rules_field_required(4900)
    assert nv.parse_version("0.5.1") < nv.parse_version(nv.MIN_PEER_VERSION)


def _peer(rules, version="0.5.3"):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Hh(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"node": f"http://127.0.0.1:{self.server.server_address[1]}", "chain_id": "netlithic-dev-1",
                    "version": version}
            if rules is not None:
                body["points_v3_rules"] = rules
            b = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), Hh)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("rules,deadline_passed,ok", [
    (None, False, True),                      # before the deadline a missing field is still tolerated (warns)
    (None, True, False),                      # from the enforcement height: refused (it used to be let through)
    (123, True, False),                       # not a string
    ("", True, False),
    ("0" * 64, False, False),                 # a present, different fingerprint is always refused
    ("same", True, True),
])
def test_m1_hello_requires_the_rules_fingerprint_after_the_deadline(node, monkeypatch, rules, deadline_passed, ok):
    monkeypatch.setattr(nv, "RULES_FIELD_REQUIRED_HEIGHT", 0 if deadline_passed else 10 ** 9)
    srv = _peer(core.points_v3_rules_hash() if rules == "same" else rules)
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert (status == 200) is ok, body
        if not ok:
            assert status == 400 and ("fingerprint" in body["error"].lower() or "rules" in body["error"].lower())
    finally:
        srv.shutdown()


def test_m1_runbook_corrects_the_split_height_and_names_0_5_2():
    rb = read_private_doc("docs", "UPGRADE_RUNBOOK_0.5.1.md")
    assert "split happens at **6440**" in rb and "0.5.3" in rb
    assert "forks the node at **6440**" in rb
    assert "7880, which was wrong" in rb


def test_m1_healthcheck_expects_0_5_2():
    assert H.DEFAULTS["expected_version"] == "0.5.3"


# =========================================================================================== M2
def _mined_chain(tmp_path, monkeypatch, n=4):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    b = Blockchain()
    for _ in range(n):
        b.chain.append(build_block(b, [core.make_reward_tx(MINER, 10 * NLT)]))
    return b, [x.to_dict() for x in b.chain]


def test_m2_check_chain_passes_a_really_mined_chain_and_catches_each_kind_of_tampering(tmp_path, monkeypatch):
    b, chain = _mined_chain(tmp_path, monkeypatch)
    assert vp.check_chain(chain) == []
    t = copy.deepcopy(chain); t[2]["transactions"][0]["amount"] += 1                 # contents changed, hash not
    assert vp.check_chain(t) and "hash does not match" in vp.check_chain(t)[0]
    t = copy.deepcopy(chain); t[3]["previous_hash"] = "0" * 64
    assert "does not link" in vp.check_chain(t)[0]
    t = copy.deepcopy(chain); t[2]["index"] = 9
    assert vp.check_chain(t)
    t = copy.deepcopy(chain); del t[1]
    assert vp.check_chain(t)
    # a block that was edited and re-hashed but has no proof of work behind it
    t = copy.deepcopy(chain)
    t[3]["transactions"][0]["amount"] += 1
    t[3]["hash"] = b.calculate_hash(index=3, timestamp=t[3]["timestamp"], transactions=t[3]["transactions"],
                                    previous_hash=t[3]["previous_hash"], nonce=t[3]["nonce"], difficulty=t[3]["difficulty"])
    bad = vp.check_chain(t)
    assert bad and ("proof of work" in bad[0] or "does not link" in bad[0])
    assert vp.check_chain([]) and vp.check_chain("x")


def test_m2_check_chain_catches_a_forged_transfer_signature(tmp_path, monkeypatch, wallets):
    b, chain = _mined_chain(tmp_path, monkeypatch, n=1)
    alice, bob = wallets
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    tx["payload"]["amount"] += 1                                                       # signature no longer matches
    blk = build_block(b, [core.make_reward_tx(MINER, 10 * NLT), tx])
    chain.append(blk.to_dict())
    bad = vp.check_chain(chain)
    assert bad and "signature" in bad[0]


def test_m2_full_validator_runs_in_a_scratch_directory(tmp_path, monkeypatch):
    b, chain = _mined_chain(tmp_path, monkeypatch, n=2)
    before = sorted(os.listdir(tmp_path))
    assert vp.check_chain(chain, full=True) == []
    assert sorted(os.listdir(tmp_path)) == before


def test_m2_paid_versus_recomputed_mismatch_is_a_warning_and_a_match_is_not():
    ind = {"status": "computed", "pays_at_height": 7880, "payouts": {"a": 5, "b": 7}}
    mk = lambda pays: [{"index": 7880, "transactions": [{"type": "payout", "recipient": a, "amount": v} for a, v in pays.items()]}]
    lines, warns = vp.independent_checks(mk({"a": 5, "b": 7}), ind)
    assert warns == [] and any("match exactly" in l for l in lines)
    lines, warns = vp.independent_checks(mk({"a": 5, "b": 8}), ind)
    assert any("DIFFER" in w for w in warns)
    _, warns = vp.independent_checks(mk({"a": 5}), ind)
    assert any("DIFFER" in w for w in warns)
    _, warns = vp.independent_checks(mk({}), ind)                                      # paid nothing, not paused: wrong
    assert any("DIFFER" in w for w in warns)
    lines, warns = vp.independent_checks(mk({}), ind, paused=True)                     # paid nothing, paused: fine
    assert warns == []
    lines, warns = vp.independent_checks([{"index": 100, "transactions": []}], ind)    # not reached yet: a proposal
    assert warns == [] and any("not reached" in l for l in lines)


def test_m2_independent_maths_equals_the_consensus_maths_registry_off(bc, tpl, monkeypatch):
    use_key(monkeypatch, key())
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)
    synth(bc, 9319, second_period_traffic(tpl), early_miner)
    chain = [b.to_dict() for b in bc.chain]
    econ = bc.econ
    ind = vp.independent_period(chain, 2, econ, None)
    cons = {p["address"]: p["amount_base_units"] for p in bc.payout_preview(2)["payouts"]}
    assert ind["status"] == "computed" and cons and ind["payouts"] == cons
    assert sum(1 for a in cons if a in set(tr.RING)) > 100        # the ring really is in the comparison


def test_m2_independent_maths_equals_the_consensus_maths_with_a_registry(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    synth(bc, 9319, second_period_traffic(tpl), early_miner, reg={FORK + 1: [rtx(k, HONEST, reason="testers")]})
    chain = [b.to_dict() for b in bc.chain]
    elig = vp._registry_eligible(chain, bc.econ)(7879)
    ind = vp.independent_period(chain, 2, bc.econ, elig)
    cons = {p["address"]: p["amount_base_units"] for p in bc.payout_preview(2)["payouts"]}
    assert ind["payouts"] == cons and all(a in cons for a in HONEST) and not (set(tr.RING) & set(cons))


def test_m2_independent_maths_equals_the_consensus_maths_on_the_first_period_with_mining_only(bc, tpl, monkeypatch):
    use_key(monkeypatch, key())
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)
    ms = [addr("m", i) for i in range(70)]
    synth(bc, 7879, lambda i: [tx_from(tpl, addr("u", i % 9), addr("s", i % 4))] if i % 5 == 0 else [],
          lambda i: ms[i % 70] if i >= FORK else early_miner(i))
    chain = [b.to_dict() for b in bc.chain]
    ind = vp.independent_period(chain, 1, bc.econ, None)
    cons = {p["address"]: p["amount_base_units"] for p in bc.payout_preview(1)["payouts"]}
    assert ind["payouts"] == cons and len(cons) == 70


def test_m2_the_scripts_hard_coded_constants_equal_the_consensus_constants():
    assert vp.R_FORK == core.POINTS_V4_HEIGHT
    assert vp.R_TX_POT_PERCENT == c.TX_POT_PERCENT
    assert vp.R_POT_CAP_PERCENT == c.POT_CAP_PERCENT
    assert vp.R_PAIR_CAP == eco.load_economics().point_pair_cap
    assert vp.R_MIN_AGE_BLOCKS == c.MIN_ADDRESS_AGE_BLOCKS
    assert vp.R_LAG_PERIODS == c.PAYOUT_LAG_PERIODS
    assert vp.R_SENDER_CAP == c.SENDER_TX_CAP_PER_EPOCH


def test_m2_limits_are_printed_and_labelled(capsys):
    assert any("independent" in x.lower() for x in vp.LIMITS) and any(x.startswith("NOT independent") for x in vp.LIMITS)
    assert "independent" in (vp.__doc__ or "").lower()


def test_m2_over_cap_check_is_per_pot_and_a_missing_fingerprint_is_an_error():
    pays = [{"address": f"a{i}", "amount_nlt": "5", "amount_base_units": 5 * NLT, "mining_points": 10, "transfer_points": 0}
            for i in range(40)]
    prev = {"status": "proposed", "period": 1, "window_start": 5000, "window_end": 6440, "pays_at_height": 7880,
            "total_nlt": "0", "payouts": pays,
            "pots": {"release_budget_nlt": "1000", "mining_pot_nlt": "800", "transfer_pot_nlt": "200",
                     "per_address_cap_percent": 2, "per_address_cap_mining_nlt": "10", "per_address_cap_transfer_nlt": "2",
                     "family_rule": False}}
    prev["payouts"][0].update(amount_nlt="11", amount_base_units=11 * NLT)              # over the MINING limit of 10
    _, warns = vp.analyse(prev)
    assert any("a0" in w and "limit" in w for w in warns)
    prev["payouts"][0].update(amount_nlt="5", amount_base_units=5 * NLT, transfer_points=3)      # mining + transfer limit is 12
    prev["payouts"][1].update(amount_nlt="11", amount_base_units=11 * NLT, mining_points=0, transfer_points=5)   # transfer-only: limit is 2
    _, warns = vp.analyse(prev)
    assert any("a1" in w and "limit" in w for w in warns)
    _, warns = vp.analyse(prev, node_rules=None, local_rules="a" * 64)
    assert any(w.startswith("ERROR") and "fingerprint" in w for w in warns)
    _, warns = vp.analyse(prev, node_rules="b" * 64, local_rules="a" * 64)
    assert any("fingerprint" in w.lower() or "rules" in w.lower() for w in warns)


def test_m2_main_stops_with_exit_2_on_a_broken_chain_and_a_missing_fingerprint(monkeypatch, capsys, tmp_path):
    _, chain = _mined_chain(tmp_path, monkeypatch, n=3)
    status = {"points_rules": "a" * 64}
    answers = {"/status": status, "/chain": {"chain": chain}}

    def fake_get(url, timeout=60):
        for k_, v in answers.items():
            if url.endswith(k_) or k_ + "?" in url:
                return v
        raise OSError("no")
    monkeypatch.setattr(vp, "_get", fake_get)
    broken = copy.deepcopy(chain); broken[2]["transactions"][0]["amount"] += 1
    answers["/chain"] = {"chain": broken}
    assert vp.main(["--node", "http://x"]) == 2
    assert "fails its own checks" in capsys.readouterr().out
    answers["/chain"] = {"chain": chain}
    answers["/status"] = {}
    assert vp.main(["--node", "http://x"]) == 2
    assert "fingerprint" in capsys.readouterr().out


# =========================================================================================== M3
def _big_chain(n):
    """n cheap synthetic blocks (no PoW): enough to time the preview."""
    chain, prev = [], "0" * 64
    miners = [addr("bm", i) for i in range(5000)]
    for i in range(n):
        h = hashlib.sha256(f"b{i}".encode()).hexdigest()
        txs = [{"type": "reward", "recipient": miners[i % 5000], "amount": 0, "fees": 0}]
        chain.append(Block(index=i, timestamp=1_700_000_000.0 + 60 * i, transactions=txs, previous_hash=prev, nonce=0,
                           difficulty=2, hash=h))
        prev = h
    return chain


def test_m3_preview_is_cached_per_period_and_tip_and_computed_on_the_snapshot(bc, tpl, monkeypatch):
    synth(bc, 9319, second_period_traffic(tpl, ring=False), early_miner)
    calls = []
    real = bc.payout_preview
    monkeypatch.setattr(bc, "payout_preview", lambda period=None, chain=None: (calls.append(period), real(period, chain))[1])
    snap = list(bc.chain)
    a = bc.payout_preview_cached(2, snap)
    b = bc.payout_preview_cached(2, snap)
    assert a is b and calls == [2]
    bc.payout_preview_cached(1, snap)
    assert calls == [2, 1]
    # the chain moved: a new answer, not a stale one
    bc.chain = bc.chain + [Block(index=9320, timestamp=1.0, transactions=[core.make_reward_tx(MINER, 1)],
                                 previous_hash=bc.chain[-1].hash, nonce=0, difficulty=2, hash="f" * 64)]
    bc.payout_preview_cached(2)
    assert calls == [2, 1, 2]
    for p in range(3, 3 + 2 * bc.PREVIEW_CACHE_SIZE):
        bc.payout_preview_cached(p, snap)
    assert len(bc._preview_cache) <= bc.PREVIEW_CACHE_SIZE


def test_m3_preview_never_reads_self_chain_when_given_a_snapshot(bc, tpl):
    synth(bc, 9319, second_period_traffic(tpl, ring=False), early_miner)
    snap = list(bc.chain)
    expected = bc.payout_preview(2, snap)
    bc.chain = None                      # any read of self.chain would now blow up
    assert bc.payout_preview(2, snap) == expected


def test_m3_preview_uses_the_registry_as_of_the_pay_block(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    reg = {FORK + 1: [rtx(k, HONEST, nonce=1)], 8000: [rtx(k, [], HONEST[:3], nonce=2)]}      # removal AFTER the period
    synth(bc, 9319, second_period_traffic(tpl, ring=False), early_miner, reg=reg)
    prev = {p["address"]: p["amount_base_units"] for p in bc.payout_preview(2)["payouts"]}
    assert prev == pay_9320(bc) and all(a in prev for a in HONEST[:3])


def test_m3_endpoint_rate_limits_get_and_does_not_hold_the_state_lock(node, monkeypatch):
    import p2p_http
    monkeypatch.setattr(p2p_http, "RATE_LIMIT_LOOPBACK", True)
    monkeypatch.setattr(p2p_http, "PREVIEW_RATE_BURST", 3.0)
    monkeypatch.setattr(p2p_http, "PREVIEW_RATE_PER_SECOND", 0.0)
    p2p_http._preview_buckets.clear()
    codes = []
    for _ in range(5):
        try:
            with urllib.request.urlopen(f"{node.self_url()}/payout_preview", timeout=5) as r:
                codes.append(r.status)
        except urllib.error.HTTPError as e:
            codes.append(e.code)
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    p2p_http._preview_buckets.clear()
    # the heavy part runs with the lock free: while the (slow) preview computes, another thread can take state_lock
    started, release = threading.Event(), threading.Event()
    real = p2p_http.blockchain.payout_preview

    def slow(period=None, chain=None):
        started.set()
        release.wait(5)
        return real(period, chain)
    monkeypatch.setattr(p2p_http.blockchain, "payout_preview", slow)
    out = {}
    t = threading.Thread(target=lambda: out.update(code=urllib.request.urlopen(f"{node.self_url()}/payout_preview?period=1", timeout=10).status))
    t.start()
    assert started.wait(5)
    got = p2p_http.state_lock.acquire(timeout=1)          # would deadlock/time out if the handler still held it
    assert got
    p2p_http.state_lock.release()
    release.set()
    t.join(10)
    assert out["code"] == 200


def test_m3_preview_at_150000_synthetic_blocks_is_fast_and_the_second_call_is_instant(bc, monkeypatch):
    use_key(monkeypatch, key())                       # registry ON: the preview also scans the whole chain for updates
    chain = _big_chain(150_000)
    t0 = time.time()
    first = bc.payout_preview_cached(None, chain)
    cold = time.time() - t0
    t1 = time.time()
    second = bc.payout_preview_cached(None, chain)
    warm = time.time() - t1
    print(f"\npayout_preview at 150,000 blocks: cold {cold:.2f}s, cached {warm * 1000:.2f} ms")
    assert first is second and first["status"] in ("paid", "skipped_or_empty", "proposed") and warm < 0.05
    assert cold < 60


# =========================================================================================== LOW
def test_low_the_period_payout_docstring_is_true_registry_as_of_the_period_end():
    doc = Blockchain._v4_period_payouts.__doc__ or ""
    assert "as of block `end`" in doc and "MAX_REORG_DEPTH" in doc
    assert "node policy and not a consensus rule" in doc          # the reviewed docstring called the reorg limit a guarantee


def test_low_reorg_record_survives_a_restart_and_counts_rejected_deep_reorgs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = Blockchain()
    b.record_reorg(2, 10)
    b.record_reorg(4, 12)
    b.record_rejected_reorg(800)
    again = Blockchain()
    info = again.reorg_info()
    assert info["max_reorg_depth_24h"] == 4 and info["rejected_deep_reorgs_24h"] == 1
    assert info["max_rejected_reorg_depth_24h"] == 800
    (tmp_path / f"{core.CHAIN_FILE}.reorgs.json").write_text("{ damaged")          # a damaged file must not stop the node
    assert Blockchain().reorg_info()["max_reorg_depth_24h"] == 0


def test_low_depth_three_alerts_in_both_the_node_warning_and_the_healthcheck(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.chdir(tmp_path)
    b = Blockchain()
    with caplog.at_level(logging.WARNING):
        b.record_reorg(2, 10)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        b.record_reorg(3, 11)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert H.deep_reorg_problem({"max_reorg_depth_24h": 2}) is None
    assert H.deep_reorg_problem({"max_reorg_depth_24h": 3}) is not None
    assert H.deep_reorg_problem({"rejected_deep_reorgs_24h": 1, "max_rejected_reorg_depth_24h": 900}) is not None
    assert H.deep_reorg_problem({}) is None and H.deep_reorg_problem(None) is None


def test_low_the_reorg_sidecar_files_are_git_ignored():
    gi = open(os.path.join(ROOT, ".gitignore")).read()
    assert "chain.json.reorgs.json" in gi
