"""Attack tests for points / payouts after block 5000 (overnight review 2026-10-23).

Part A  unit level: real contributions.points_from_blocks with the real shipped
        constants (period 1440, sender cap 30, pair cap 10, min 1 NLT, new-address
        wait 1440, reciprocal block) on small hand-built block lists.
Part B  payout level: real Blockchain.expected_payouts on a synthetic chain
        (blocks placed directly in bc.chain; transfer signatures are copied from one
        throwaway wallet because points/payout code never verifies signatures).
Part C  consensus level: real mined blocks accepted by real validation, scaled constants.

Tests named test_finding_* pin weaknesses that are still open. The two payout-level findings
(Sybil ring, pool front-loading) were closed by points v3 (POINTS_V4_HEIGHT): their v2 behaviour is kept as
test_legacy_v2_* (v3 forced off) and the closed behaviour is asserted by test_closed_* below and in test_points_v3.py.
"""
import hashlib
import time
from dataclasses import replace

import pytest

import contributions as c
import core
import economics as eco
import pause
from core import Block, Blockchain

NLT = eco.BASE_UNITS_PER_NLT
FORK = 5000
PERIOD = 1440
EPOCH_FIRST = FORK                     # first 1440-block period is [5000, 6440)


def addr(i: int) -> str:
    return hashlib.sha256(f"sybil-{i}".encode()).hexdigest()[:32]


def xfer(sender, recipient, amount=NLT, nonce=1):
    return {"payload": {"sender": sender, "recipient": recipient, "amount": amount, "nonce": nonce,
                        "timestamp": 1.0, "fee": 10_000, "chain_id": "netlithic-dev-1"},
            "pubkey": "00" * 32, "sig": "00" * 64}


def blk(idx, miner, transfers=()):
    return {"index": idx, "transactions": [{"type": "reward", "recipient": miner, "amount": 0, "fees": 0},
                                           *transfers]}


E = eco.load_economics()


def pts(blocks, first_seen=None, rules=FORK):
    seen = first_seen if first_seen is not None else {}
    return c.points_from_blocks(blocks, E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id,
                                rules_height=rules, first_seen=seen)


def cp(users, a):
    return users.get(a, {}).get("cp_tenths", 0)


MINER = "ff" * 16


def old(*addrs):
    """first_seen: all given addresses first received coins at block 1 (long past the wait)."""
    return {a: 1 for a in addrs}


def test_constants_match_the_announced_rules():
    assert E.epoch_length_at(FORK) == PERIOD and E.is_epoch_block(6440) and not E.is_epoch_block(6439)
    assert E.points_per_block == 25 and E.points_per_tx == 10
    assert E.min_point_transfer == NLT and E.point_pair_cap == 10 and E.payout_cap == 25_000 * NLT
    assert c.SENDER_TX_CAP_PER_EPOCH == 30 and c.MIN_ADDRESS_AGE_BLOCKS == 1440
    assert E.vesting_epochs == 6 and E.min_fee == 10_000


# ---------------- Part A: rule edges ----------------

def test_sender_cap_30_per_period_and_resets_next_period():
    s = addr(0)
    recips = [addr(100 + i) for i in range(40)]
    b = [blk(FORK + i, MINER, [xfer(s, recips[i])]) for i in range(35)]
    assert cp(pts(b, old(s)), s) == 30 * 10                                 # 31st..35th earn nothing
    nxt = FORK + PERIOD
    b += [blk(nxt + i, MINER, [xfer(s, recips[i])]) for i in range(5)]
    assert cp(pts(b, old(s)), s) == 35 * 10                                 # new period: counter reset


def test_pair_cap_10_per_period_then_other_recipient_still_earns():
    s, r1, r2 = addr(0), addr(1), addr(2)
    b = [blk(FORK + i, MINER, [xfer(s, r1)]) for i in range(12)] + [blk(FORK + 20, MINER, [xfer(s, r2)])]
    assert cp(pts(b, old(s)), s) == 11 * 10


def test_reciprocal_blocked_both_orders_but_three_cycle_is_not():
    a, b_, c_ = addr(1), addr(2), addr(3)
    fs = old(a, b_, c_)
    u = pts([blk(FORK, MINER, [xfer(a, b_)]), blk(FORK + 1, MINER, [xfer(b_, a)])], fs)
    assert (cp(u, a), cp(u, b_)) == (10, 0)
    u = pts([blk(FORK, MINER, [xfer(b_, a)]), blk(FORK + 1, MINER, [xfer(a, b_)])], fs)
    assert (cp(u, a), cp(u, b_)) == (0, 10)
    # same block, both directions: first in order wins
    u = pts([blk(FORK, MINER, [xfer(a, b_), xfer(b_, a)])], fs)
    assert (cp(u, a), cp(u, b_)) == (10, 0)
    # A->B->C->A: nobody is blocked
    u = pts([blk(FORK, MINER, [xfer(a, b_), xfer(b_, c_), xfer(c_, a)])], fs)
    assert (cp(u, a), cp(u, b_), cp(u, c_)) == (10, 10, 10)


def test_reciprocal_block_resets_each_period():
    a, b_ = addr(1), addr(2)
    u = pts([blk(FORK, MINER, [xfer(a, b_)]), blk(FORK + PERIOD, MINER, [xfer(b_, a)])], old(a, b_))
    assert cp(u, b_) == 10


def test_minimum_transfer_and_self_transfer():
    s, r = addr(1), addr(2)
    u = pts([blk(FORK, MINER, [xfer(s, r, NLT - 1), xfer(s, s, 5 * NLT), xfer(s, r, NLT)])], old(s))
    assert cp(u, s) == 10                                                   # only the exactly-1-NLT one


def test_new_address_wait_boundary_is_exactly_1440_blocks():
    s, r = addr(1), addr(2)
    fs = {s: 5000}
    u = pts([blk(5000 + 1439, MINER, [xfer(s, r)])], fs)
    assert cp(u, s) == 0
    u = pts([blk(5000 + 1440, MINER, [xfer(s, r)])], fs)
    assert cp(u, s) == 10
    # an address never seen receiving anything can't earn (but can't send either)
    assert cp(pts([blk(6000, MINER, [xfer(s, r)])], {}), s) == 0


def test_burst_across_the_period_boundary_is_allowed():
    """30 at the very end of one period plus 30 at the start of the next = 60 in ~2 blocks. By design."""
    s = addr(1)
    end, start = FORK + PERIOD - 1, FORK + PERIOD
    b = [blk(end, MINER, [xfer(s, addr(100 + i)) for i in range(30)]),
         blk(start, MINER, [xfer(s, addr(200 + i)) for i in range(30)])]
    assert cp(pts(b, old(s)), s) == 600


def test_rules_inactive_before_fork():
    a, b_ = addr(1), addr(2)
    u = pts([blk(FORK - 2, MINER, [xfer(a, b_)]), blk(FORK - 1, MINER, [xfer(b_, a)])], {}, rules=FORK)
    assert cp(u, a) == 10 and cp(u, b_) == 10                               # old rules: no reciprocal/age


# ---------------- Part A: abuse attempts ----------------

def ring_blocks(n, start, offsets=(1, 2, 3), per_pair=10, first_height=None):
    """n addresses; each sends `per_pair` x 1 NLT to each of i+offsets. One tx per block (round-robin)."""
    ring = [addr(i) for i in range(n)]
    txs = [xfer(ring[i], ring[(i + o) % n]) for o in offsets for _ in range(per_pair) for i in range(n)]
    blocks = [blk(start + j, MINER, [t]) for j, t in enumerate(txs)]
    return ring, blocks


def test_finding_circulant_ring_earns_the_full_30_for_every_fake_address():
    ring, blocks = ring_blocks(8, FORK)
    assert len(blocks) == 8 * 30 and blocks[-1]["index"] < FORK + PERIOD
    u = pts(blocks, old(*ring))
    assert all(cp(u, a) == 300 for a in ring)                               # 8 fakes x 30 points each


def test_per_pair_and_reciprocal_rules_stop_a_two_address_ping_pong():
    a, b_ = addr(1), addr(2)
    blocks = [blk(FORK + i, MINER, [xfer(a, b_), xfer(b_, a)]) for i in range(100)]
    u = pts(blocks, old(a, b_))
    assert (cp(u, a), cp(u, b_)) == (100, 0)                                # 10 pts max for one side, 0 other


def test_finding_empty_block_rule_bypassed_by_one_dust_transfer_to_an_own_address():
    """Rule: a block with no transfers, or only miner->miner transfers, earns no block points.
    A miner who adds ONE transfer of 1 base unit to a second address of his own (fee goes back to
    him as the miner) gets 2.5 points for every block. The transfer itself earns nothing."""
    m, m2 = addr(1), addr(2)
    empty = [blk(FORK + i, m) for i in range(10)]
    self_only = [blk(FORK + i, m, [xfer(m, m, 5 * NLT)]) for i in range(10)]
    dust = [blk(FORK + i, m, [xfer(m, m2, 1)]) for i in range(10)]
    assert cp(pts(empty, old(m)), m) == 0
    assert cp(pts(self_only, old(m)), m) == 0
    assert cp(pts(dust, old(m)), m) == 10 * 25
    assert cp(pts(dust, old(m)), m) == cp(pts([blk(FORK + i, m, [xfer(addr(9), addr(8), NLT)]) for i in range(10)], old(m)), m)


def test_third_party_transfer_in_block_makes_it_count_even_if_sent_by_the_miner_himself_to_a_friend():
    m = addr(1)
    assert cp(pts([blk(FORK, m, [xfer(addr(5), addr(6))])], {}), m) == 25


def test_dust_ring_members_cannot_farm_via_transfer_points():
    """The same dust transfers do not make transfer points: min 1 NLT."""
    s = addr(1)
    u = pts([blk(FORK + i, MINER, [xfer(s, addr(100 + i), NLT - 1)]) for i in range(30)], old(s))
    assert cp(u, s) == 0


def test_address_case_and_whitespace_variants_cannot_split_a_pair_counter():
    """Blocks only ever hold normalized (lowercase, trimmed, 32 hex) addresses."""
    from core import normalize_signed_tx
    t = xfer(addr(1), addr(2))
    t["payload"]["recipient"] = "  " + addr(2).upper() + " "
    assert normalize_signed_tx(t)["payload"]["recipient"] == addr(2)
    t["payload"]["recipient"] = addr(2) + "0"
    with pytest.raises(ValueError):
        normalize_signed_tx(t)


# ---------------- Part B: payouts on a synthetic chain with real constants ----------------

def synth(bc, upto, per_block_txs, miner_for):
    """Blocks 1..upto appended straight to bc.chain. per_block_txs(i) -> list of transfer dicts."""
    chain = [bc.chain[0]]
    for i in range(1, upto + 1):
        txs = [core.make_reward_tx(miner_for(i), 10 * NLT)] + [dict(t) for t in per_block_txs(i)]
        chain.append(Block(index=i, timestamp=1_700_000_000.0 + i * 60, transactions=txs,
                           previous_hash=chain[-1].hash, nonce=0,
                           difficulty=2 if i < FORK else core.LWMA_MIN_WORK,
                           hash=hashlib.sha256(f"s{i}".encode()).hexdigest()))
    bc.chain = chain


@pytest.fixture
def bc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    return Blockchain()


def real_signed(wallets):
    alice, bob = wallets
    return alice.create_transaction(bob.address, NLT, nonce=1)


def tx_from_template(tpl, sender, recipient, amount=NLT):
    t = {"payload": dict(tpl["payload"]), "pubkey": tpl["pubkey"], "sig": tpl["sig"]}
    t["payload"].update(sender=sender, recipient=recipient, amount=amount)
    return t


def test_payout_rules_hold_on_real_chain_object(bc, wallets, monkeypatch):
    tpl = real_signed(wallets)
    ring = [addr(i) for i in range(8)]
    # who mines is irrelevant here: a different single miner address for every block
    seq = [(ring[i], ring[(i + o) % 8]) for o in (1, 2, 3) for _ in range(10) for i in range(8)]
    start = FORK + 5

    def txs(i):
        j = i - start
        return [tx_from_template(tpl, *seq[j])] if 0 <= j < len(seq) else []

    # every ring address received something at block 1, long before the wait
    def miner(i):
        return ring[i - 1] if i <= 8 else MINER

    synth(bc, 7879, txs, miner)
    pays = bc.expected_payouts(bc.chain, 7880)
    got = {p["recipient"]: p["amount"] for p in pays}
    assert set(ring) <= set(got)
    assert sum(got.values()) <= bc.community_pool_available(bc.chain, 6441)
    assert all(a <= 25_000 * NLT for a in got.values())


FAUCET = addr(50_000)


@pytest.fixture(autouse=True)
def _issuer_list(monkeypatch):
    """The real list names addresses that do not exist in these synthetic chains; use a faucet that does."""
    monkeypatch.setattr(c, "FAMILY_ISSUERS", (FAUCET,))
    monkeypatch.setattr(c, "FAMILY_RULE_ENABLED", True)      # these tests are about the family rule when it is on


def sybil_world(bc, wallets, n_sybil, n_honest, honest_tx_each=5, miners=3, upto=7879):
    """Real shipped economics. In period [5000,6440): n_sybil fake addresses in a circulant ring
    (30 pts each), n_honest honest users sending honest_tx_each transfers to distinct merchants,
    `miners` honest miners sharing the blocks. Under points v4 the period [5000,6440) is paid at block 7880 (one-day
    lag), so the chain runs to 7879 by default; the legacy v2 tests pass upto=6439."""
    tpl = real_signed(wallets)
    sy = [addr(i) for i in range(n_sybil)]
    honest = [addr(10_000 + i) for i in range(n_honest)]
    merch = [addr(20_000 + i) for i in range(honest_tx_each)]
    mine_addrs = [addr(30_000 + i) for i in range(miners)]
    seq = []
    offs = (1, 2, 3)
    if n_sybil >= 8:
        seq += [(sy[i], sy[(i + o) % n_sybil]) for o in offs for _ in range(10) for i in range(n_sybil)]
    seq += [(h, m) for h in honest for m in merch]
    # spread the transfers evenly over the period, at most a handful per block
    slots = {}
    for j, pair in enumerate(seq):
        slots.setdefault(FORK + (j * PERIOD) // max(1, len(seq)), []).append(pair)

    def txs(i):
        return [tx_from_template(tpl, *p) for p in slots.get(i, [])]

    def miner(i):
        return mine_addrs[i % miners]

    synth(bc, upto, txs, miner)
    # every actor "has been around" before the fork. Honest users and merchants receive a block-1 reward
    # (own family); the fake addresses are all funded by ONE hub address with a transfer in block 2.
    hub, faucet = addr(40_000), FAUCET
    early = Block(index=bc.chain[1].index, timestamp=bc.chain[1].timestamp,
                  transactions=bc.chain[1].transactions + [
                      {"type": "reward", "recipient": a, "amount": 0, "fees": 0} for a in [hub, faucet] + honest + merch],
                  previous_hash=bc.chain[1].previous_hash, nonce=0, difficulty=2,
                  hash=bc.chain[1].hash)
    bc.chain[1] = early
    b2 = bc.chain[2]
    bc.chain[2] = Block(index=2, timestamp=b2.timestamp, transactions=b2.transactions + [
        tx_from_template(tpl, hub, a) for a in sy] + [tx_from_template(tpl, faucet, merch[0])],
        previous_hash=b2.previous_hash, nonce=0,
        difficulty=b2.difficulty, hash=b2.hash)
    return sy, honest, mine_addrs


def test_legacy_v2_sybil_ring_takes_most_of_the_pool_and_beats_the_per_address_cap(bc, wallets, monkeypatch):
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 10 ** 9)               # v2 rules, as shipped in 0.5.0
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=120, n_honest=20, upto=6439)
    pool = bc.community_pool_available(bc.chain, 6440)
    assert pool == 2_500_000 * NLT                                         # first month-step is fully available on day 1
    pays = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 6440)}
    sybil_total = sum(pays.get(a, 0) for a in sy)
    honest_total = sum(pays.get(a, 0) for a in honest)
    miner_total = sum(pays.get(a, 0) for a in miners)
    # 120 fake addresses x 30 pts = 3600; 20 honest x 5 pts = 100; miners get 2.5 x 1440 = 3600 (blocks with txs only)
    assert sybil_total > 10 * honest_total
    assert sybil_total > 0.9 * 0.4 * pool                                   # a large share of the pool
    # per-address cap is per address: every fake one gets its own 25,000 allowance
    assert max(pays[a] for a in sy) <= 25_000 * NLT
    assert sum(pays.values()) <= pool


def test_legacy_v2_pool_month_step_is_mostly_paid_out_on_day_one_and_cap_hits_real_miners_not_fakes(bc, wallets, monkeypatch):
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 10 ** 9)               # v2 rules, as shipped in 0.5.0
    """Each period pays pool * share (capped per address); nothing limits how much of the
    available pool one period may pay out. 400 fake addresses (each far below the cap) plus
    3 honest miners (each ABOVE the cap): the fakes take ~everything that is paid, and
    ~80% of the 2.5M month-step (which is meant to last 121 days at 60 s blocks) goes on
    day one; the rest is drained within days because the excess carries over."""
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=400, n_honest=0, upto=6439)
    pool = bc.community_pool_available(bc.chain, 6440)
    pays = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 6440)}
    paid = sum(pays.values())
    sybil_paid = sum(pays.get(a, 0) for a in sy)
    miner_paid = sum(pays.get(a, 0) for a in miners)
    assert paid > 0.7 * pool
    assert sybil_paid > 0.9 * paid
    assert all(pays[a] == E.payout_cap for a in miners)                 # honest heavy contributors are capped
    assert E.blocks_per_month == 175_200                                # next month-step is 121 days away at 60 s


def test_closed_sybil_ring_gets_nothing_and_honest_users_are_paid(bc, wallets):
    """Same world as the legacy finding, now at the real first payout (7880: period 1 paid with the one-day lag), family rule on."""
    assert core.is_points_v3(6440)
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=120, n_honest=20)
    pool = bc.community_pool_available(bc.chain, 6441)
    pays = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 7880)}
    assert sum(pays.get(a, 0) for a in sy) == 0                            # one funding family: no transfer points
    assert all(pays.get(a, 0) > 0 for a in honest)
    assert sum(pays.values()) <= pool * PERIOD // E.blocks_per_month       # one period never pays more than its slice


def test_closed_pool_is_released_slowly_and_400_fakes_cannot_drain_it(bc, wallets):
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=400, n_honest=0)
    pool = bc.community_pool_available(bc.chain, 6441)
    pays = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 7880)}
    paid = sum(pays.values())
    assert sum(pays.get(a, 0) for a in sy) == 0
    assert 0 < paid <= pool * PERIOD // E.blocks_per_month < pool // 80    # about 0.82% of the pool a day, not 80%
    assert set(pays) == set(miners)                                        # only the three real miners were paid


def test_payouts_never_exceed_pool_or_cap_property(bc, wallets):
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=0, n_honest=7)
    pool = bc.community_pool_available(bc.chain, 6441)
    pays = bc.expected_payouts(bc.chain, 7880)
    assert pays and sum(p["amount"] for p in pays) <= pool
    assert all(0 < p["amount"] <= E.payout_cap for p in pays)
    assert [p["recipient"] for p in pays] == sorted(p["recipient"] for p in pays)   # deterministic order


# ---------------- Part C: real mined blocks, real validation, scaled constants ----------------

def _econ():
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id="netlithic-dev-1",
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    return replace(eco.load_economics(), **base)


@pytest.fixture
def small(tmp_path, monkeypatch, wallets):
    from wallet import Wallet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 3)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    alice, bob = wallets
    carol = Wallet.create("carol")
    bc = Blockchain(econ=_econ())

    def mine(miner, txs=()):
        b = bc.mine_block(miner.address if hasattr(miner, "address") else miner, list(txs))
        assert bc.add_block(b.to_dict()), bc.last_reject_reason
        return b

    def points():
        e = bc.econ
        return c.points_from_blocks((b.to_dict() for b in bc.chain), e.points_per_block, e.points_per_tx,
                                    e.min_point_transfer, e.point_pair_cap, e.epoch_blocks, epoch_fn=e.epoch_id,
                                    rules_height=core.SECURITY_FORK_HEIGHT,
                                    first_seen=c.first_seen_heights(b.to_dict() for b in bc.chain))

    return bc, mine, points, alice, bob, carol


def test_finding_three_cycle_is_valid_on_chain_and_every_member_earns(small):
    bc, mine, points, alice, bob, carol = small
    mine(alice); mine(alice)                                     # 1, 2
    mine(alice, [alice.create_transaction(bob.address, 5 * NLT, nonce=1)])      # 3  (epoch A: funding)
    mine(alice, [alice.create_transaction(carol.address, 5 * NLT, nonce=2)])    # 4
    mine(alice); mine(alice)                                     # 5, 6
    # next period (blocks 7..10): ring alice -> bob -> carol -> alice, all in one block
    mine(alice, [alice.create_transaction(bob.address, NLT, nonce=3),
                 bob.create_transaction(carol.address, NLT, nonce=1),
                 carol.create_transaction(alice.address, NLT, nonce=1)])        # 7
    u = points()
    ring_points = {n: u[w.address]["point_tx_count"] for n, w in (("alice", alice), ("bob", bob), ("carol", carol))}
    assert ring_points["bob"] == 1 and ring_points["carol"] == 1 and ring_points["alice"] == 3   # 2 funding + 1 ring
    # and the reverse of one ring edge is what the reciprocal rule blocks
    mine(bob, [bob.create_transaction(alice.address, NLT, nonce=2)])            # 8: bob -> alice after alice -> bob
    assert points()[bob.address]["point_tx_count"] == 1                        # unchanged: blocked
    assert bc.is_valid_chain(bc.to_list())


def test_finding_real_miner_gets_block_points_with_a_dust_self_transfer_between_own_addresses(small):
    bc, mine, points, alice, bob, carol = small
    mine(alice); mine(alice)
    base = points()[alice.address]["cp_tenths"]
    assert base == 0                                              # two empty self-mined blocks: nothing
    mine(alice, [alice.create_transaction(bob.address, 1, nonce=1)])            # 1 base unit to "my other address"
    u = points()[alice.address]
    assert u["cp_tenths"] == 25 and u["point_tx_count"] == 0
    assert bc.is_valid_chain(bc.to_list())
