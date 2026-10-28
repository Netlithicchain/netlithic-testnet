"""Registered contributors for the transfer pot (TEMPORARY, centralized). Runs locally only; throwaway keys made in the tests.

Part 1  the registry module on its own (signatures, shape, delay, removal, limits)
Part 2  real constants (fork 5000, 1,440-block period) on synthetic chains: who gets paid at 9320
Part 3  attacks (unlisted ring, many fakes via the process, empty key, pause, boundaries)
Part 4  scaled constants, several in-process nodes: real blocks, mempool, reorg
Part 5  /registry over HTTP and the explorer page
"""
import hashlib
import json
import time
import urllib.request
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import contributions as c
import core
import economics as eco
import pause
import registry
from conftest import build_block, post
from core import Block, Blockchain
from wallet import pubkey_from_private

pytestmark = pytest.mark.registry          # these tests run with the registry ON (as shipped)

NLT = eco.BASE_UNITS_PER_NLT
FORK = 5000
PERIOD = 1440
CID = "netlithic-dev-1"
MINER = "ff" * 16
MINER_B = "b2" * 16


def addr(tag, i=0) -> str:
    return hashlib.sha256(f"reg-{tag}-{i}".encode()).hexdigest()[:32]


def key() -> str:
    return Ed25519PrivateKey.generate().private_bytes_raw().hex()


def use_key(monkeypatch, k: str) -> str:
    pub = pubkey_from_private(k)
    monkeypatch.setattr(registry, "REGISTRY_PUBKEY", pub)
    return pub


def rtx(k, adds=(), removes=(), nonce=1, reason="ok", cid=CID, valid_until=None):
    return registry.sign_registry_tx(k, list(adds), list(removes), reason, nonce, cid, valid_until)


def xfer(sender, recipient, amount=NLT, nonce=1):
    return {"payload": {"sender": sender, "recipient": recipient, "amount": amount, "nonce": nonce,
                        "timestamp": 1.0, "fee": 10_000, "chain_id": CID},
            "pubkey": "00" * 32, "sig": "00" * 64}


def reward(miner):
    return {"type": "reward", "recipient": miner, "amount": 10 * NLT, "fees": 0}


def chain_of(upto, extra=None, miner_for=None):
    """Plain dict blocks 0..upto. extra: {height: [txs]}"""
    extra = extra or {}
    miner_for = miner_for or (lambda i: MINER)
    return [{"index": h, "transactions": ([reward(miner_for(h))] if h else []) + list(extra.get(h, []))}
            for h in range(upto + 1)]


def period_of(h):
    return (h - FORK) // PERIOD if h >= FORK else -1


def scan(blocks, **kw):
    return registry.scan(blocks, period_of, fork_height=FORK, chain_id=CID, **kw)


# =============================================================== Part 1: the module

def test_signature_roundtrip_and_every_tamper_is_rejected(monkeypatch):
    k = key()
    pub = use_key(monkeypatch, k)
    good = rtx(k, adds=[addr("a")], reason="reviewed by the founder")
    assert registry.signature_ok(good, CID)
    assert registry.normalize_registry_tx(good) == good                       # already canonical
    for bad in (dict(good, reason="edited"), dict(good, adds=[addr("b")]), dict(good, nonce=2),
                dict(good, removes=[addr("c")]), dict(good, sig="0" * 128), dict(good, pubkey=pubkey_from_private(key()))):
        assert not registry.signature_ok(bad, CID)
    assert not registry.signature_ok(good, "netlithic-other")                # another chain
    assert not registry.signature_ok(rtx(key(), adds=[addr("a")]), CID)      # another key
    assert registry.key_id() == hashlib.sha256(bytes.fromhex(pub)).hexdigest()[:16]


def test_a_pause_signature_can_never_be_a_registry_signature(monkeypatch):
    """Same key on purpose here: the two messages are domain-separated, so a pause signature proves nothing."""
    k = key()
    use_key(monkeypatch, k)
    p = pause.make_pause_tx(k, True, "x", 1, CID)
    forged = {"type": "registry", "adds": [addr("a")], "removes": [], "reason": "x", "nonce": 1,
              "pubkey": p["pubkey"], "sig": p["sig"]}
    assert not registry.signature_ok(forged, CID)
    msg = registry.registry_message(registry.registry_payload([addr("a")], [], "x", 1, CID))
    assert msg != pause.pause_message(pause.pause_payload(True, "x", 1, CID))


def test_empty_key_slot_accepts_nothing(monkeypatch):
    k = key()
    assert registry.REGISTRY_PUBKEY == ""                                     # the shipped slot is empty
    tx = rtx(k, adds=[addr("a")])
    assert not registry.signature_ok(tx, CID)
    st = registry.RegistryState(period_of)
    assert not st.check_and_apply(tx, FORK, CID)
    assert registry.key_id() == ""


@pytest.mark.parametrize("bad", [
    dict(adds=["xyz"]), dict(adds=[addr("a").upper()]), dict(adds=[addr("a"), addr("a")]),
    dict(adds=[], removes=[]), dict(adds=[addr("a")], removes=[addr("a")]), dict(reason=""),
    dict(reason="x" * 281), dict(nonce=0), dict(adds=[addr("p", i) for i in range(51)]), dict(adds="notalist"),
])
def test_malformed_updates_are_rejected_by_the_shape_check(monkeypatch, bad):
    k = key()
    base = dict(adds=[addr("a")], removes=[], reason="ok", nonce=1)
    base.update(bad)
    tx = {"type": "registry", **base, "pubkey": "00" * 32, "sig": "00" * 64}
    with pytest.raises(ValueError):
        registry.normalize_registry_tx(tx)


def test_delay_boundary_is_exact(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = addr("a")
    st = scan(chain_of(FORK + 10, {FORK + 5: [rtx(k, [a])]}))
    h = FORK + 5
    assert not st.eligible(a, h) and not st.eligible(a, h + 1439)
    assert st.eligible(a, h + 1440) and st.eligible(a, h + 99999)
    assert st.pending(h + 10) == {a: h + 1440} and st.pending(h + 1440) == {}


def test_removal_and_readd_restart_the_clock(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = addr("a")
    blocks = chain_of(FORK + 4000, {FORK + 1: [rtx(k, [a], nonce=1)], FORK + 2000: [rtx(k, [], [a], nonce=2)],
                                   FORK + 3000: [rtx(k, [a], nonce=3)]})
    st = scan(blocks, upto=FORK + 2500)
    # a removal is prospective: it stops earning REMOVE_DELAY (1,440) blocks after it, earlier transfers keep their points
    assert st.eligible(a, FORK + 2400) and st.eligible(a, FORK + 2000 + 1439) and not st.eligible(a, FORK + 2000 + 1440)
    st = scan(blocks)
    assert not st.eligible(a, FORK + 1440) and st.eligible(a, FORK + 1441)    # first listed at +1 -> counts from +1441
    assert st.nonce == 3 and not st.eligible(a, FORK + 3000 + 1439) and st.eligible(a, FORK + 3000 + 1440)
    assert [e["adds"] or e["removes"] for e in st.log] == [[a], [a], [a]]


def test_nonce_sequence_double_add_and_unknown_remove(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    st = registry.RegistryState(period_of)
    a, b = addr("a"), addr("b")
    assert not st.check_and_apply(rtx(k, [a], nonce=2), FORK, CID)           # gap
    assert st.check_and_apply(rtx(k, [a], nonce=1), FORK, CID)
    assert not st.check_and_apply(rtx(k, [a], nonce=1), FORK, CID)           # replay
    assert not st.check_and_apply(rtx(k, [a], nonce=2), FORK, CID)           # already listed
    assert not st.check_and_apply(rtx(k, [], [b], nonce=2), FORK, CID)       # never listed
    assert st.check_and_apply(rtx(k, [b], [a], nonce=2), FORK, CID)
    assert st.nonce == 2 and set(st.registered_now()) == {b}


def test_noncanonical_form_is_rejected(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    st = registry.RegistryState(period_of)
    good = rtx(k, [addr("a")])
    assert not st.check_and_apply(dict(good, junk="x" * 500), FORK, CID)
    assert not st.check_and_apply(dict(good, pubkey=good["pubkey"].upper()), FORK, CID)
    assert st.check_and_apply(good, FORK, CID)


def test_per_period_and_total_limits(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    st = registry.RegistryState(period_of)
    n = 0
    for i in range(1, 11):                                                   # 10 x 5 = 50 adds in period 0
        assert st.check_and_apply(rtx(k, [addr("f", n + j) for j in range(5)], nonce=i), FORK + i, CID)
        n += 5
    assert not st.check_and_apply(rtx(k, [addr("f", 99)], nonce=11), FORK + 20, CID)      # 51st in the window
    # rolling window: the first period boundary does NOT reset the allowance (no double allowance across it)
    assert not st.check_and_apply(rtx(k, [addr("f", 99)], nonce=11), FORK + PERIOD, CID)
    assert st.check_and_apply(rtx(k, [addr("f", 99)], nonce=11), FORK + 10 + PERIOD, CID)  # the oldest add left the window
    assert not st.check_and_apply(rtx(k, [], [addr("zz")], nonce=12), FORK + PERIOD, CID)  # unlisted
    # the 500-entry ceiling
    monkeypatch.setattr(registry, "MAX_ADDS_PER_WINDOW", 10_000)
    st = registry.RegistryState(period_of)
    for i in range(10):
        assert st.check_and_apply(rtx(k, [addr("g", i * 50 + j) for j in range(50)], nonce=i + 1), FORK + i, CID)
    assert not st.check_and_apply(rtx(k, [addr("g", 9999)], nonce=11), FORK + 11, CID)
    assert st.check_and_apply(rtx(k, [], [addr("g", 0)], nonce=11), FORK + 11, CID)       # removal frees a slot


def test_disabled_registry_accepts_no_updates_and_resolver_is_open(monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)
    st = registry.RegistryState(period_of)
    assert not st.check_and_apply(rtx(k, [addr("a")]), FORK, CID)
    assert registry.resolver(st, FORK)(addr("nobody"), FORK + 5) is True


def test_sunset_height_opens_the_pot_again(monkeypatch):
    use_key(monkeypatch, key())
    monkeypatch.setattr(registry, "REGISTRY_SUNSET_HEIGHT", FORK + 3000)
    ok = registry.resolver(registry.RegistryState(period_of), FORK)
    assert ok(addr("nobody"), FORK + 2999) is False                           # empty list: fail closed
    assert ok(addr("nobody"), FORK + 3000) is True                            # announced end
    assert ok(addr("nobody"), FORK - 1) is True                               # before the fork: old rules


# =============================================================== Part 2: real constants

@pytest.fixture
def bc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    return Blockchain()


def synth(bc, upto, per_block=None, miner_for=None, reg=None, pause_at=None):
    """Blocks 1..upto appended straight to bc.chain (no PoW). per_block(i) -> transfers, reg {h: [registry tx]}."""
    per_block, miner_for = per_block or (lambda i: []), miner_for or (lambda i: MINER)
    reg, pause_at = reg or {}, pause_at or {}
    chain = [bc.chain[0]]
    for i in range(1, upto + 1):
        txs = [core.make_reward_tx(miner_for(i), 10 * NLT)]
        txs += [pause.normalize_pause_tx(t) for t in pause_at.get(i, [])]
        txs += list(reg.get(i, []))
        txs += [dict(t) for t in per_block(i)]
        chain.append(Block(index=i, timestamp=1_700_000_000.0 + i * 60, transactions=txs,
                           previous_hash=chain[-1].hash, nonce=0,
                           difficulty=2 if i < FORK else core.LWMA_MIN_WORK,
                           hash=hashlib.sha256(f"r{i}".encode()).hexdigest()))
    bc.chain = chain


def tx_from(tpl, sender, recipient, amount=NLT):
    t = {"payload": dict(tpl["payload"]), "pubkey": tpl["pubkey"], "sig": tpl["sig"]}
    t["payload"].update(sender=sender, recipient=recipient, amount=amount)
    return t


@pytest.fixture
def tpl(wallets):
    alice, bob = wallets
    return alice.create_transaction(bob.address, NLT, nonce=1)


HONEST = [addr("hon", i) for i in range(10)]
MERCH = [addr("merch", i) for i in range(3)]
RING = [addr("ring", i) for i in range(400)]
KNOWN = HONEST + MERCH + RING


def early_miner(i):
    """Every known address received a reward at block 1..N: long past the 1,440-block new-address wait."""
    return KNOWN[i - 1] if i <= len(KNOWN) else MINER


def second_period_traffic(tpl, ring=True):
    """Honest users pay merchants and a 400-fake ring pays itself, all in the second period [6440, 7880) (paid at 9320)."""
    plan = {}
    seq = [(h, m) for h in HONEST for m in MERCH for _ in range(2)]
    if ring:
        seq += [(RING[i], RING[(i + o) % 400]) for o in (1, 2, 3) for i in range(400) for _ in range(10)]
    for j, (s, r) in enumerate(seq):
        plan.setdefault(6440 + 10 + (j * 1400) // len(seq), []).append(tx_from(tpl, s, r))
    return lambda i: plan.get(i, [])


def pay_9320(bc):
    pays = bc.expected_payouts(bc.chain, 9320)
    return {p["recipient"]: p["amount"] for p in pays}


def test_only_listed_senders_earn_transfer_points_and_the_rest_stays_in_the_pool(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    listed = rtx(k, HONEST, reason="first ten testers, reviewed")
    synth(bc, 9319, second_period_traffic(tpl), early_miner, reg={FORK + 1: [listed]})
    got = pay_9320(bc)
    assert all(got.get(a, 0) > 0 for a in HONEST)                             # listed + 1 day old: paid
    assert all(a not in got for a in RING)                                    # 400 unlisted fakes: nothing
    assert all(a not in got for a in MERCH)                                   # receivers earn no transfer points
    budget = bc._v4_period_inputs(bc.chain, 6440, 7880)[1]
    assert sum(got.values()) <= budget * 20 // 100 + budget * 80 // 100
    assert sum(got.values()) < budget * 20 // 100 + 1                         # no mining in this chain except MINER (cap 2%)


def test_same_chain_with_registry_off_is_exactly_v4_and_the_ring_is_paid(bc, tpl, monkeypatch):
    use_key(monkeypatch, key())
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)
    synth(bc, 9319, second_period_traffic(tpl), early_miner)
    got = pay_9320(bc)
    assert sum(got.get(a, 0) for a in RING) > 0                               # v4 does not stop the ring
    assert all(got.get(a, 0) > 0 for a in HONEST)


def test_mining_pot_stays_open_to_unlisted_miners(bc, tpl, monkeypatch):
    use_key(monkeypatch, key())
    noise = lambda i: [tx_from(tpl, addr("noise"), MERCH[0])]                  # an unlisted sender's transfer fills the block
    synth(bc, 9319, noise, lambda i: addr("m", i % 3))
    got = pay_9320(bc)
    assert set(got) == {addr("m", 0), addr("m", 1), addr("m", 2)}             # nobody is listed, miners still paid
    assert all(v > 0 for v in got.values())


def test_pending_addition_earns_nothing_until_the_day_is_up(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = addr("late")
    # added at 7000: transfers at 7000..8439 do not count; the window [6440,7880) therefore sees nothing from `a`
    seq = {h: [tx_from(tpl, a, MERCH[0])] for h in range(6500, 7880, 7)}
    synth(bc, 9319, lambda i: seq.get(i, []), lambda i: a if i == 1 else MINER, reg={7000: [rtx(k, [a])]})
    got = pay_9320(bc)
    assert a not in got
    # the same list but added at 5001 pays it
    synth(bc, 9319, lambda i: seq.get(i, []), lambda i: a if i == 1 else MINER, reg={5001: [rtx(k, [a])]})
    assert pay_9320(bc).get(a, 0) > 0


def test_removal_is_prospective_it_never_erases_an_earned_unpaid_window(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = addr("hon", 0)
    seq = {h: [tx_from(tpl, a, MERCH[0])] for h in range(6500, 7800, 7)}
    # removed at 9000, i.e. AFTER the period was earned and BEFORE it is paid at 9320: still paid (H2 fix)
    reg = {5001: [rtx(k, [a], nonce=1)], 9000: [rtx(k, [], [a], nonce=2)]}
    synth(bc, 9319, lambda i: seq.get(i, []), early_miner, reg=reg)
    assert pay_9320(bc).get(a, 0) > 0
    # removing an address that was still waiting for its one day (listed 5001 -> counts from 6441; removed 6000) cancels it
    reg = {5001: [rtx(k, [a], nonce=1)], 6000: [rtx(k, [], [a], nonce=2)]}
    synth(bc, 9319, lambda i: seq.get(i, []), early_miner, reg=reg)
    assert a not in pay_9320(bc)
    # a removal in the paying block or after does not touch that payout either
    synth(bc, 9319, lambda i: seq.get(i, []), early_miner, reg={5001: [rtx(k, [a], nonce=1)]})
    assert pay_9320(bc).get(a, 0) > 0
    # an already paid period is never rewritten: removal at 9400 leaves the 9320 list as it was
    before = pay_9320(bc)
    synth(bc, 9399, lambda i: seq.get(i, []), early_miner, reg={5001: [rtx(k, [a], nonce=1)], 9319: []})
    assert bc.expected_payouts(bc.chain[:9320], 9320) and pay_9320_from(bc, 9320) == before


def pay_9320_from(bc, h):
    return {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain[:h], h)}


def test_rules_fingerprint_covers_the_registry(monkeypatch):
    base = core.points_v3_rules_hash()
    use_key(monkeypatch, key())
    with_key = core.points_v3_rules_hash()
    assert with_key != base
    monkeypatch.setattr(registry, "REGISTRY_DELAY_BLOCKS", 1000)
    assert core.points_v3_rules_hash() != with_key
    monkeypatch.setattr(registry, "REGISTRY_DELAY_BLOCKS", 1440)
    monkeypatch.setattr(registry, "MAX_ADDS_PER_WINDOW", 49)
    assert core.points_v3_rules_hash() != with_key
    monkeypatch.setattr(registry, "MAX_ADDS_PER_WINDOW", 50)
    for name, val in (("MAX_REMOVES_PER_WINDOW", 49), ("REGISTRY_REMOVE_DELAY_BLOCKS", 1000),
                      ("REGISTRY_ROTATION_DELAY_BLOCKS", 1000), ("REGISTRY_LIMIT_WINDOW", 1000)):
        old = getattr(registry, name)
        monkeypatch.setattr(registry, name, val)
        assert core.points_v3_rules_hash() != with_key, name
        monkeypatch.setattr(registry, name, old)
    monkeypatch.setattr(registry, "REGISTRY_SUNSET_HEIGHT", 99999)
    assert core.points_v3_rules_hash() != with_key


def test_registry_disabled_fingerprint_is_exactly_the_v4_one(monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)
    off = core.points_v3_rules_hash()
    use_key(monkeypatch, key())
    assert core.points_v3_rules_hash() == off                                 # the key does not matter when off
    parts = ["rules=points-v4", "block_points=flat", f"fork_height={core.POINTS_V4_HEIGHT}", "tx_pot_percent=20", "pot_cap_percent=2",
             "payout_lag_periods=1", "family_rule=0", f"family_pair_cap={c.FAMILY_PAIR_CAP}",
             f"max_base_per_point={c.MAX_BASE_PER_POINT}", f"min_point_transfer={c.MIN_POINT_TRANSFER}", "issuers=-",
             f"pause_key={pause.PAUSE_PUBKEY or '-'}", f"pause_expiry={pause.PAUSE_EXPIRY_BLOCKS}",
             f"pause_max_reason={pause.MAX_REASON}"]   # 0.5.3 reviewer fix P2: the pause switch is part of the fingerprint
    assert off == hashlib.sha256("|".join(parts).encode()).hexdigest()        # the v4 recipe, rebuilt here by hand


def test_handshake_refuses_a_peer_with_a_different_registry_setting(monkeypatch):
    """The /hello check compares the rules fingerprint, so a node with another key or delay is turned away."""
    use_key(monkeypatch, key())
    mine = core.points_v3_rules_hash()
    monkeypatch.setattr(registry, "REGISTRY_PUBKEY", pubkey_from_private(key()))
    assert core.points_v3_rules_hash() != mine


# =============================================================== Part 3: attacks

def test_ring_of_400_unlisted_fakes_earns_nothing_from_transfers_even_with_a_listed_funder(bc, tpl, monkeypatch):
    """One listed address funds/pays the whole ring: only the LISTED sender's own transfers can earn, and they are
    capped (30 per period, 10 per pair, 2% of the pot per address), so the ring cannot be paid through one door."""
    k = key()
    use_key(monkeypatch, k)
    door = HONEST[0]
    plan = {}
    for j in range(400 * 10):
        plan.setdefault(6450 + (j * 1400) // 4000, []).append(tx_from(tpl, door, RING[j % 400]))
    for j in range(400 * 10):                                                 # and the fakes pay each other
        plan.setdefault(6450 + (j * 1400) // 4000, []).append(tx_from(tpl, RING[j % 400], RING[(j + 1) % 400]))
    synth(bc, 9319, lambda i: plan.get(i, []), early_miner, reg={FORK + 1: [rtx(k, [door])]})
    got = pay_9320(bc)
    assert all(a not in got for a in RING)
    pts = bc.contribution_points(bc.chain[6440:7880])
    assert pts[door]["point_tx_count"] == c.SENDER_TX_CAP_PER_EPOCH           # the door earns its 30, no more
    assert all(pts.get(a, {}).get("point_tx_count", 0) == 0 for a in RING)


def test_attacker_with_the_key_can_only_add_fifty_a_period_and_each_waits_a_day(bc, tpl, monkeypatch):
    """A stolen/abused key: the most it can list is 50 addresses per 1,440 blocks, visible for a day first."""
    k = key()
    use_key(monkeypatch, k)
    fakes = RING[:200]
    st = registry.RegistryState(period_of)
    ok = [st.check_and_apply(rtx(k, fakes[i * 10:(i + 1) * 10], nonce=i + 1), FORK + i, CID) for i in range(20)]
    assert ok == [True] * 5 + [False] * 15                                     # 5 x 10 = 50, then the cap bites
    assert len(st.registered_now()) == 50
    # inside the first period all 50 are still pending when the period's payout view is taken
    assert len(st.pending(FORK + 100)) == 50
    # and they can still be removed by the key holder (or by shipping a new key with the next release)
    assert st.check_and_apply(rtx(k, [], fakes[:10], nonce=6), FORK + 200, CID)
    # worst case in a payout: 50 listed fakes can earn at most 20% of the budget in total (pot cap), never more
    plan = {h: [tx_from(tpl, f, MERCH[0]) for f in fakes[:50]] for h in range(6460, 7860, 40)}
    synth(bc, 9319, lambda i: plan.get(i, []), early_miner,
          reg={FORK + 1: [rtx(k, fakes[:10], nonce=1)], FORK + 2: [rtx(k, fakes[10:20], nonce=2)],
               FORK + 3: [rtx(k, fakes[20:30], nonce=3)], FORK + 4: [rtx(k, fakes[30:40], nonce=4)],
               FORK + 5: [rtx(k, fakes[40:50], nonce=5)]})
    budget = bc._v4_period_inputs(bc.chain, 6440, 7880)[1]
    got = pay_9320(bc)
    assert 0 < sum(got.get(a, 0) for a in fakes[:50]) <= budget * 20 // 100


def test_key_empty_nobody_earns_transfer_points_no_crash_pot_stays_in_pool(bc, tpl):
    assert registry.REGISTRY_PUBKEY == "" and registry.REGISTRY_ENABLED
    traffic = second_period_traffic(tpl)
    synth(bc, 9319, lambda i: traffic(i) + [tx_from(tpl, addr("noise"), MERCH[0])], lambda i: addr("m", i % 3))
    got = pay_9320(bc)
    assert all(a not in got for a in RING + HONEST)                           # nobody earns from transfers
    assert set(got) == {addr("m", 0), addr("m", 1), addr("m", 2)}             # mining is still paid
    budget = bc._v4_period_inputs(bc.chain, 6440, 7880)[1]
    assert sum(got.values()) <= budget * 80 // 100                            # the transfer pot is unspent
    info = bc.registry_info()
    assert info["mode"] == "no_key" and not info["key_set"] and info["active_count"] == 0
    assert "pool" in info["note"]
    # nothing is "open by accident": no registry tx is valid either
    k = key()
    assert not bc.validate_candidate_tx_against_chain_and_mempool(rtx(k, [HONEST[0]]), [])


def test_listed_but_empty_list_fails_closed_not_open(bc, tpl, monkeypatch):
    use_key(monkeypatch, key())
    traffic = second_period_traffic(tpl)
    synth(bc, 9319, lambda i: traffic(i) + [tx_from(tpl, addr("noise"), MERCH[0])], lambda i: addr("m", i % 3))
    assert all(a not in pay_9320(bc) for a in RING)
    assert bc.registry_info()["mode"] == "empty"


def test_registry_update_during_a_pause_is_accepted_payouts_stay_paused_clock_keeps_running(bc, tpl, monkeypatch):
    k, pk = key(), key()
    use_key(monkeypatch, k)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pubkey_from_private(pk))
    ptx = pause.make_pause_tx(pk, True, "drill", 1, CID)
    a = HONEST[0]
    seq = {h: [tx_from(tpl, a, MERCH[0])] for h in range(6500, 7800, 9)}
    synth(bc, 9318, lambda i: seq.get(i, []), early_miner, pause_at={9000: [ptx]})
    assert pause.state(bc.chain)["paused"]
    assert pay_9320(bc) == {}                                                  # paused at 9320: nothing paid
    # a registry update is mined while paused: it is a valid block
    blk = bc.mine_block(MINER_B, [rtx(k, [a], reason="added while paused")])
    assert blk.index == 9319 and any(registry.is_registry_tx(t) for t in blk.transactions)
    assert bc.add_block(blk.to_dict()), bc.last_reject_reason
    assert bc.registry_state().nonce == 1 and pause.state(bc.chain)["paused"]
    # the day runs in blocks, pause or not
    assert bc.registry_state().pending(9320) == {a: 9319 + 1440}


def test_boundary_heights_for_the_update_itself(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    mp = []
    # synth up to 4998 -> next block is 4999: a registry tx is not valid before the fork
    synth(bc, 4998, miner_for=lambda i: MINER)
    t = rtx(k, [HONEST[0]])
    assert not bc.validate_candidate_tx_against_chain_and_mempool(t, mp)
    bad = build_block(bc, [core.make_reward_tx(MINER_B, bc.expected_reward(bc.chain)), t])
    assert bad.index == 4999 and not bc.add_block(bad.to_dict())
    ok = bc.mine_block(MINER_B, [t])
    assert ok.index == 4999 and not any(registry.is_registry_tx(x) for x in ok.transactions)
    assert bc.add_block(ok.to_dict()), bc.last_reject_reason
    assert bc.validate_candidate_tx_against_chain_and_mempool(t, mp)           # next block is 5000
    b5000 = bc.mine_block(MINER_B, [t])
    assert b5000.index == 5000 and any(registry.is_registry_tx(x) for x in b5000.transactions)
    assert bc.add_block(b5000.to_dict()), bc.last_reject_reason
    assert registry.RegistryState and bc.registry_state().registered_now() == {HONEST[0]: 5000}
    # a transfer at 5000+1439 does not earn, at 5000+1440 it does (checked on the resolver the payout uses)
    ok = registry.resolver(bc.registry_state(), FORK)
    assert not ok(HONEST[0], 6439) and ok(HONEST[0], 6440)


def test_transfer_at_the_window_edges(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    a = HONEST[0]
    # window [6440, 7880): transfers at 6440 and 7879 count, at 6439 and 7880 they belong to other periods
    seq = {6439: [tx_from(tpl, a, MERCH[0])], 6440: [tx_from(tpl, a, MERCH[1])],
           7879: [tx_from(tpl, a, MERCH[2])], 7880: [tx_from(tpl, a, MERCH[0], 2 * NLT)]}
    synth(bc, 9319, lambda i: seq.get(i, []), early_miner, reg={FORK: [rtx(k, [a])]})
    users = bc.contribution_points(bc.chain[6440:7880])
    assert users[a]["point_tx_count"] == 2


def test_registry_tx_ordering_and_garbage_in_blocks(bc, tpl, monkeypatch):
    k = key()
    use_key(monkeypatch, k)
    synth(bc, 5000, miner_for=lambda i: MINER)
    good = rtx(k, [HONEST[0]])
    rew = core.make_reward_tx(MINER_B, bc.expected_reward(bc.chain))
    # registry tx after a signed transfer: wrong place
    send = tpl
    assert not bc.add_block(build_block(bc, [rew, send, good]).to_dict())
    # forged signature
    assert not bc.add_block(build_block(bc, [rew, rtx(key(), [HONEST[0]])]).to_dict())
    # a replayed nonce / a skipped nonce
    assert not bc.add_block(build_block(bc, [rew, rtx(k, [HONEST[0]], nonce=2)]).to_dict())
    # 51 adds in one update, and 2 updates that together exceed the period cap
    forged = build_block(bc, [rew, good]).to_dict()                              # an oversized update smuggled into a peer's block dict
    forged["transactions"][1] = dict(good, adds=sorted(addr("x", i) for i in range(51)))
    assert not bc.add_block(forged)
    # the good one is fine, and stored in canonical form (extra fields cannot ride along)
    mal = dict(good, junk="x" * 1000)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(mal, [])                           # the mempool refuses a tx with extra fields
    d = build_block(bc, [rew, good]).to_dict()
    d["transactions"][1] = mal                                                   # a block parses to the canonical form: junk is dropped
    assert bc.add_block(d), bc.last_reject_reason
    assert bc.chain[-1].transactions[1] == good
    assert registry.is_registry_tx(bc.chain[-1].transactions[1])
    assert bc.registry_info()["pending"]


# =============================================================== Part 4: scaled nodes, real blocks

def _econ():
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id=CID,
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    return replace(eco.load_economics(), **base)


@pytest.fixture
def small(tmp_path, monkeypatch, wallets):
    """Fork at 3, payout period 4 blocks, registry delay 2 blocks. Three nodes with identical genesis."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, CID, (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 3)
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 3)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    monkeypatch.setattr(registry, "REGISTRY_DELAY_BLOCKS", 2)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    k = key()
    use_key(monkeypatch, k)
    return [Blockchain(econ=_econ()) for _ in range(3)], k, wallets


def mine_all(nodes, miner, txs=(), via=0, expect_all=True):
    blk = nodes[via].mine_block(miner, list(txs))
    res = [n.add_block(blk.to_dict()) for n in nodes]
    if expect_all:
        assert all(res), [n.last_reject_reason for n in nodes]
    return blk, res


def test_three_nodes_agree_listed_sender_is_paid_unlisted_is_not(small):
    nodes, k, (alice, bob) = small
    cid = nodes[0].econ.chain_id
    mine_all(nodes, bob.address)                                              # 1  bob mines (funds)
    mine_all(nodes, alice.address)                                            # 2  alice mines (funds)
    mine_all(nodes, "ee" * 16, [rtx(k, [alice.address], reason="reviewed")], via=1)   # 3 (fork block): list alice
    # alice (listed from block 5) and bob (not listed) both pay a third party, blocks 4..6
    mine_all(nodes, "ee" * 16, [bob.create_transaction("ab" * 16, NLT, nonce=1)], via=2)       # 4
    mine_all(nodes, "ee" * 16, [alice.create_transaction("cd" * 16, NLT, nonce=1)], via=0)     # 5  counts
    mine_all(nodes, "ee" * 16, [alice.create_transaction("cd" * 16, NLT, nonce=2),
                                bob.create_transaction("ab" * 16, NLT, nonce=2)], via=1)       # 6  alice counts
    for n in nodes:
        assert n.registry_state().registered_now() == {alice.address: 3}
    mine_all(nodes, "ee" * 16)                                                # 7 (payout block: nothing yet, lag)
    mine_all(nodes, "ee" * 16)                                                # 8
    mine_all(nodes, "ee" * 16)                                                # 9
    mine_all(nodes, "ee" * 16)                                                # 10
    # block 11 pays [3,7): alice earns transfer points, bob does not, on every node
    outs = [{p["recipient"] for p in n.expected_payouts(n.chain, 11)} for n in nodes]
    assert outs[0] == outs[1] == outs[2]
    assert alice.address in outs[0] and bob.address not in outs[0]
    blk, _ = mine_all(nodes, "ee" * 16, via=2)
    assert blk.index == 11 and any(t.get("recipient") == alice.address for t in blk.transactions if t.get("type") == "payout")
    assert nodes[0].is_valid_chain(nodes[0].to_list())
    assert len({n.chain[-1].hash for n in nodes}) == 1


def test_block_with_wrong_payouts_for_the_registry_view_is_rejected(small):
    """A miner that pays the open (v4) list while the registry is on builds an invalid block: nodes agree on who is paid."""
    nodes, k, (alice, bob) = small
    mine_all(nodes, bob.address)
    mine_all(nodes, alice.address)
    mine_all(nodes, "ee" * 16)                                                # 3
    mine_all(nodes, "ee" * 16, [bob.create_transaction("ab" * 16, NLT, nonce=1)])   # 4  bob unlisted
    for _ in range(6):
        mine_all(nodes, "ee" * 16)                                            # 5..10
    n = nodes[0]
    honest = n.expected_payouts(n.chain, 11)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(registry, "REGISTRY_ENABLED", False)
        open_list = n.expected_payouts(n.chain, 11)                           # what an "open pot" node would pay
    assert {p["recipient"] for p in open_list} != {p["recipient"] for p in honest}
    forged = build_block(n, [core.make_reward_tx("ee" * 16, n.expected_reward(n.chain))] + open_list)
    assert not n.add_block(forged.to_dict())


def test_reorg_across_a_registry_change_follows_the_heavier_chain(small):
    nodes, k, (alice, bob) = small
    a, b, c_ = nodes
    mine_all(nodes, bob.address)
    mine_all(nodes, alice.address)
    mine_all(nodes, "ee" * 16)                                                # 3: everybody agrees up to here
    # fork: node a adds alice to the list (block 4); node b mines two plain blocks (4, 5)
    blkA = a.mine_block("ee" * 16, [rtx(k, [alice.address], reason="branch A")])
    assert a.add_block(blkA.to_dict())
    assert a.registry_state().registered_now() == {alice.address: 4}
    for _ in range(2):
        assert b.add_block(b.mine_block("dd" * 16, []).to_dict())
    assert b.registry_state().registered_now() == {}
    # a hears about the heavier chain and switches: the list rolls back with it, nothing is left over
    assert a.replace_chain(b.to_list()), a.last_reject_reason
    assert a.registry_state().registered_now() == {} and a.registry_state().nonce == 0
    assert alice.address not in a.registry_info()["active"] and a.registry_info()["pending_count"] == 0
    # the same update (same nonce) can be mined again on the new chain, and the other way round also works
    assert a.validate_candidate_tx_against_chain_and_mempool(rtx(k, [alice.address], reason="branch A"), [])
    blk = a.mine_block("ee" * 16, [rtx(k, [alice.address], reason="branch A")])
    assert a.add_block(blk.to_dict())
    for _ in range(2):
        assert b.add_block(b.mine_block("dd" * 16, []).to_dict())
    b.add_block(blk.to_dict())                                                # b stays on its own (heavier or equal) chain
    assert b.registry_state().registered_now() == {}
    assert a.replace_chain(b.to_list()) and a.registry_state().registered_now() == {}


def test_mempool_takes_one_registry_update_at_a_time_and_mining_skips_bad_ones(small):
    nodes, k, (alice, bob) = small
    n = nodes[0]
    for _ in range(3):
        mine_all(nodes, "ee" * 16)
    first, second = rtx(k, [alice.address], nonce=1), rtx(k, [bob.address], nonce=2)
    assert n.validate_candidate_tx_against_chain_and_mempool(first, [])
    assert not n.validate_candidate_tx_against_chain_and_mempool(second, [first])      # one at a time
    assert not n.validate_candidate_tx_against_chain_and_mempool(rtx(key(), [alice.address]), [])
    blk = n.mine_block("ee" * 16, [rtx(key(), [bob.address], nonce=1), second, first])
    regs = [t for t in blk.transactions if registry.is_registry_tx(t)]
    assert [t["nonce"] for t in regs] == [1, 2]                                         # sorted, forged one dropped
    assert n.add_block(blk.to_dict()), n.last_reject_reason


# =============================================================== Part 5: HTTP and explorer

def test_registry_endpoint_status_and_explorer_page(node, monkeypatch, wallets):
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 0)
    alice, bob = wallets
    monkeypatch.setattr(registry, "REGISTRY_DELAY_BLOCKS", 3)
    k = key()
    pub = use_key(monkeypatch, k)

    def get(path):
        with urllib.request.urlopen(f"{node.self_url()}{path}", timeout=5) as r:
            return json.loads(r.read())

    st = get("/registry")
    assert st["mode"] == "empty" and st["key_set"] and st["temporary"] and st["centralized"]
    assert get("/status")["registry_key_id"] == registry.key_id() and get("/status")["registry_enabled"] is True
    cid = node.chain_id()
    assert post(node, "/submit_tx", {"tx": rtx(k, [alice.address], cid=cid, reason="first tester", valid_until=5000)})[0] == 200
    assert post(node, "/submit_tx", {"tx": rtx(key(), [bob.address], cid=cid, reason="x", valid_until=5000)})[0] == 400
    assert post(node, "/mine", {"miner_address": bob.address})[0] == 200
    st = get("/registry")
    assert st["pending_count"] == 1 and st["pending"][0]["address"] == alice.address and st["nonce"] == 1
    assert st["log"][0]["reason"] == "first tester"
    one = get(f"/registry?address={alice.address}")["address"]
    assert one["status"] == "pending" and one["earns_transfer_points"] is False
    assert get(f"/registry?address={bob.address}")["address"]["status"] == "not_listed"
    with pytest.raises(Exception):
        get("/registry?address=nothex")
    for _ in range(registry.REGISTRY_DELAY_BLOCKS):
        assert post(node, "/mine", {"miner_address": bob.address})[0] == 200
    assert get("/registry")["active_count"] == 1 and get("/registry")["mode"] == "on"

    import explorer

    class Reader:
        def get(self, path):
            return get(path)

    page = explorer.Explorer(Reader()).registry()
    assert "Temporary and centralized" in page[1] and alice.address in page[1] and "Registered contributors" in page[1]
