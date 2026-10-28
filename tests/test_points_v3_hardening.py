"""Points v3 hardening: public issuer list, fail-safe, dust poisoning / dust evasion, "why 0" explanations."""
import hashlib
import json
import os
import urllib.request

import pytest

import contributions as c
import core
import economics as eco

NLT = eco.BASE_UNITS_PER_NLT
E = eco.load_economics()
FORK = 5000
MINER = "ff" * 16
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def A(i):
    return hashlib.sha256(f"hard-{i}".encode()).hexdigest()[:32]


def xfer(s, r, amt=NLT):
    return {"payload": {"sender": s, "recipient": r, "amount": amt, "nonce": 1, "fee": 10_000}}


def rew(a):
    return {"type": "reward", "recipient": a, "amount": 0, "fees": 0}


def blk(i, *txs, miner=MINER):
    return {"index": i, "transactions": [rew(miner), *txs]}


def run(blocks, issuers, explain=None, roots="auto"):
    if roots == "auto":
        roots = c.active_family_roots(blocks, issuers=issuers)
    return c.points_from_blocks(blocks, E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK,
                                first_seen=c.first_seen_heights(blocks), v3=True, roots=roots, explain=explain)


def tx_points(users, a):
    return users.get(a, {}).get("point_tx_count", 0)


@pytest.fixture(autouse=True)
def _family_rule_on(monkeypatch):
    monkeypatch.setattr(c, "FAMILY_RULE_ENABLED", True)     # this file tests the rule itself; the shipped default is off


# ---- 1. the public issuer list ----

def test_issuer_list_has_the_confirmed_public_addresses():
    assert "e924523b2614a39b9bb4ff8bfc05e747" in c.FAMILY_ISSUERS          # founder + faucet
    assert len(set(c.FAMILY_ISSUERS)) == len(c.FAMILY_ISSUERS)
    assert all(len(a) == 32 and int(a, 16) >= 0 for a in c.FAMILY_ISSUERS)


def test_every_genesis_allocation_address_is_listed():
    with open(os.path.join(ROOT, "config", "genesis_config.json")) as f:
        cfg = json.load(f)
    found = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "address" and isinstance(v, str):
                    found.add(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(cfg)
    assert len(found) >= 3
    assert found <= set(c.FAMILY_ISSUERS), found - set(c.FAMILY_ISSUERS)


# ---- 2. fail safe ----

def faucet_world(faucet, users=6):
    """The faucet mines, then funds `users` people who then pay each other. Nobody is related."""
    people = [A(i) for i in range(users)]
    b = [blk(1, miner=faucet)]
    b.append(blk(2, *[xfer(faucet, p, 10 * NLT) for p in people]))
    b.append(blk(3, *[xfer(people[i], people[(i + 1) % users]) for i in range(users)]))
    return b, people


def test_empty_issuer_list_means_no_family_rule():
    b, people = faucet_world(A(100))
    assert c.active_family_roots(b, issuers=()) is None


def test_list_that_matches_no_real_funder_means_no_family_rule():
    b, _ = faucet_world(A(100))
    assert c.active_family_roots(b, issuers=(A(555), A(556))) is None


def test_matching_issuer_turns_the_family_rule_on():
    b, _ = faucet_world(A(100))
    assert c.active_family_roots(b, issuers=(A(100),)) is not None


def test_wrong_issuer_list_pays_honest_users_instead_of_zero():
    """Faucet-funded users: with the right list each is its own family and earns; with a wrong list the
    rule steps aside (design A) instead of putting all of them into the faucet's family."""
    faucet = A(100)
    b, people = faucet_world(faucet)
    right = run(b, (faucet,))
    wrong = run(b, (A(555),))
    unchecked = run(b, (A(555),), roots=c.family_roots(b, issuers=(A(555),)))   # what v3 did before the fail-safe
    assert all(tx_points(right, p) == 1 for p in people)
    assert all(tx_points(wrong, p) == 1 for p in people)
    assert all(tx_points(unchecked, p) == 0 for p in people)


def test_core_uses_the_fail_safe(monkeypatch):
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 0)
    monkeypatch.setattr(c, "FAMILY_ISSUERS", ())
    chain = [core.Block(index=0, timestamp=0, transactions=[], previous_hash="0", nonce=0, difficulty=1, hash="0")]
    assert core.Blockchain._v3_args(chain, 10) == {"v3": True, "roots": None, "family_pair_cap": None}


# ---- 3. dust poisoning and dust evasion ----

def poison_world(first_amount, faucet=A(100), victims=5):
    """An attacker sends `first_amount` to each new honest address before the faucet funds them."""
    bad = A(200)
    people = [A(i) for i in range(victims)]
    b = [blk(1, miner=faucet), blk(2, miner=bad)]
    b.append(blk(3, *[xfer(bad, p, first_amount) for p in people]))
    b.append(blk(4, *[xfer(faucet, p, 10 * NLT) for p in people]))
    b.append(blk(5, *[xfer(people[i], people[(i + 1) % victims]) for i in range(victims)]))
    return b, people, faucet


@pytest.mark.parametrize("dust", [1, NLT // 2, NLT - 1])
def test_dust_below_the_minimum_cannot_poison_an_address(dust):
    b, people, faucet = poison_world(dust)
    roots = c.family_roots(b, issuers=(faucet,))
    assert all(roots[p] == p for p in people)                 # the faucet decided, not the attacker
    assert all(tx_points(run(b, (faucet,)), p) == 1 for p in people)


def test_a_full_minimum_payment_does_poison_but_it_costs_the_attacker_and_only_hits_victims_among_themselves():
    """Honest finding: an attacker who pays >= 1 NLT to a brand-new address before anyone else becomes its family.
    Damage: victims lose points only on transfers among themselves. Cost: >= 1 NLT + fee per victim."""
    b, people, faucet = poison_world(NLT)
    users = run(b, (faucet,))
    roots = c.family_roots(b, issuers=(faucet,))
    assert all(roots[p] == A(200) for p in people)
    assert all(tx_points(users, p) == 0 for p in people)
    # a victim paying someone outside the poisoned group still earns
    b2 = b + [blk(6, xfer(people[0], A(300)), miner=A(300))]
    assert tx_points(run(b2, (faucet,)), people[0]) == 1


def test_honest_payment_that_crosses_the_line_beats_earlier_dust():
    faucet, bad, p = A(100), A(200), A(1)
    b = [blk(1, miner=faucet), blk(2, miner=bad),
         blk(3, xfer(bad, p, NLT // 2)),                     # dust first
         blk(4, xfer(faucet, p, 10 * NLT))]                  # the real funding crosses 1 NLT
    assert c.family_roots(b, issuers=(faucet,))[p] == p


def test_splitting_funding_into_dust_does_not_hide_a_farm():
    """Evasion attempt: fund every fake with 3 x 0.4 NLT so that no single transfer is >= 1 NLT. The running
    total decides, so each fake still joins the hub's family."""
    hub, faucet = A(200), A(100)
    fakes = [A(i) for i in range(10)]
    b = [blk(1, miner=faucet), blk(2, xfer(faucet, A(900), 10 * NLT), miner=hub)]
    for n in range(3):
        b.append(blk(3 + n, *[xfer(hub, f, 4 * NLT // 10) for f in fakes]))
    b.append(blk(6, *[xfer(fakes[i], fakes[(i + 1) % 10]) for i in range(10)]))
    roots = c.family_roots(b, issuers=(faucet,))
    assert all(roots[f] == hub for f in fakes)
    users = run(b, (faucet,))
    assert all(tx_points(users, f) == 0 for f in fakes)


def test_raising_the_funding_minimum_would_open_a_hole():
    """Why the funding minimum stays equal to the 1 NLT point minimum: a farm only needs ~1 NLT per address,
    so any higher funding minimum lets the attacker fund fakes just below it and stay unrelated."""
    hub, faucet = A(200), A(100)
    fakes = [A(i) for i in range(10)]
    b = [blk(1, miner=faucet), blk(2, miner=hub), blk(3, *[xfer(hub, f, 3 * NLT) for f in fakes]),
         blk(4, *[xfer(fakes[i], fakes[(i + 1) % 10]) for i in range(10)])]
    strict = c.family_roots(b, min_amount=10 * NLT, issuers=(faucet,))
    assert all(strict.get(f, f) == f for f in fakes)           # unrelated: the farm earns
    assert all(c.family_roots(b, issuers=(faucet,))[f] == hub for f in fakes)


# ---- listing check ----

def test_unlisted_funders_flags_a_hidden_issuer_but_not_listed_or_small_senders():
    faucet, hidden, small = A(100), A(201), A(202)
    b = [blk(1, miner=faucet), blk(2, miner=hidden), blk(3, miner=small),
         blk(4, *[xfer(faucet, A(i), NLT) for i in range(5)],
             *[xfer(hidden, A(10 + i), NLT) for i in range(4)],
             *[xfer(small, A(20 + i), NLT) for i in range(2)])]
    assert c.unlisted_funders(b, issuers=(faucet,)) == {hidden: 4}


# ---- 4. "why did I earn 0" ----

def test_every_zero_has_a_plain_reason():
    faucet, a, b_, newbie = A(100), A(1), A(2), A(3)
    hist = [blk(1, miner=faucet), blk(2, xfer(faucet, a, 10 * NLT), xfer(faucet, b_, 10 * NLT)),
            blk(FORK + 3000, xfer(a, b_)),             # earned
            blk(FORK + 3001, xfer(b_, a)),             # reciprocal
            blk(FORK + 3002, xfer(a, a)),              # self
            blk(FORK + 3003, xfer(a, b_, NLT // 2))]   # below minimum
    log = []
    run(hist, (faucet,), explain=log)
    reasons = [x["reason"] for x in log if x["sender"] in (a, b_)]
    assert reasons == ["earned", "reciprocal", "self", "below_minimum"]
    assert set(c.REASON_TEXT) >= set(reasons)


def test_family_and_cap_reasons_are_reported():
    hub, faucet = A(200), A(100)
    k1, k2 = A(1), A(2)
    h = [blk(1, miner=faucet), blk(2, xfer(faucet, A(900), 10 * NLT), miner=hub),
         blk(3, xfer(hub, k1), xfer(hub, k2)), blk(4, xfer(k1, k2))]
    log = []
    run(h, (faucet,), explain=log)
    assert [x["reason"] for x in log if x["sender"] == k1] == ["same_family"]
    assert "same funding family" in c.REASON_TEXT["same_family"].lower()


def test_explain_points_api_says_why_in_words(node, monkeypatch):
    from wallet import Wallet
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 0)
    bc = node.blockchain
    carol, alice, bob = Wallet.create("carol"), Wallet.create("alice"), Wallet.create("bob")
    monkeypatch.setattr(c, "FAMILY_ISSUERS", (carol.address,))
    assert bc.add_block(bc.mine_block(carol.address, []).to_dict())
    assert bc.add_block(bc.mine_block(MINER, [carol.create_transaction(alice.address, 5 * NLT, 1)]).to_dict())
    assert bc.add_block(bc.mine_block(MINER, [alice.create_transaction(bob.address, 2 * NLT, 1)]).to_dict())
    assert bc.add_block(bc.mine_block(MINER, [bob.create_transaction(alice.address, NLT, 1)]).to_dict())
    with urllib.request.urlopen(f"{node.self_url()}/points_explain?address={bob.address}") as r:
        data = json.loads(r.read())
    assert data["family_rule_active"] and data["funding_family"] == alice.address
    assert data["points"] == "0" and data["transfers_earned"] == 0
    assert data["summary"] == {"same_family": 1}
    assert data["why_zero"] == ["1 transfer earned 0: " + c.REASON_TEXT["same_family"]]
    assert data["transfers"][0]["why"] == c.REASON_TEXT["same_family"]
    with urllib.request.urlopen(f"{node.self_url()}/points_explain?address={alice.address}") as r:
        assert json.loads(r.read())["summary"] == {"same_family": 1}   # alice funded bob: one family


# ---- review H1: dust-staged intermediaries ----

def staged_ring(n=12, hub=A(200), faucet=A(100), stage_amounts=(60_000_000, 60_000_000)):
    """Review PoC P2: hub -> X_k in dust pieces, X_k -> F_k, then the F_k pay each other in a ring."""
    xs = [A(300 + i) for i in range(n)]
    fk = [A(400 + i) for i in range(n)]
    b = [blk(1, miner=faucet), blk(2, xfer(faucet, A(900), 10 * NLT), miner=hub)]
    for amt in stage_amounts:
        b.append(blk(len(b) + 1, *[xfer(hub, x, amt) for x in xs]))
    b.append(blk(len(b) + 1, *[xfer(x, f, NLT) for x, f in zip(xs, fk)]))
    b.append(blk(FORK + 10, *[xfer(fk[i], fk[(i + o) % n]) for o in (1, 2, 3) for i in range(n)]))
    return b, xs, fk, hub, faucet


def test_dust_staged_intermediaries_do_not_make_unlimited_families():
    b, xs, fk, hub, faucet = staged_ring()
    roots = c.family_roots(b, issuers=(faucet,))
    assert all(roots[x] == hub for x in xs)                  # two 0.6 pieces add up to 1.2 NLT, from the hub
    assert {roots[f] for f in fk} == {hub}                   # one family, not 12
    users = run(b, (faucet,))
    assert sum(tx_points(users, f) for f in fk) == 0


def test_staging_through_senders_that_stay_unrooted_earns_nothing():
    """Variant that survives plain cumulative rooting: X holds only dust (0.7 NLT, unrooted) and tops up F_k
    to 1.2 NLT. F_k must not be rooted from X, and an unrooted address earns no transfer points."""
    hub, faucet, n = A(200), A(100), 12
    xs, fk = [A(300 + i) for i in range(n)], [A(400 + i) for i in range(n)]
    b = [blk(1, miner=faucet), blk(2, xfer(faucet, A(900), 10 * NLT), miner=hub),
         blk(3, *[xfer(hub, x, 70_000_000) for x in xs]),
         blk(4, *[xfer(hub, f, 60_000_000) for f in fk]),
         blk(5, *[xfer(x, f, 60_000_000) for x, f in zip(xs, fk)]),
         blk(FORK + 10, *[xfer(fk[i], fk[(i + o) % n]) for o in (1, 2, 3) for i in range(n)])]
    roots = c.family_roots(b, issuers=(faucet,))
    assert not any(f in roots for f in fk) and not any(x in roots for x in xs)
    users = run(b, (faucet,))
    assert sum(tx_points(users, f) for f in fk) == 0
    log = []
    run(b, (faucet,), explain=log)
    assert {x["reason"] for x in log if x["sender"] in fk} == {"unrooted"}


def test_unrooted_address_that_later_gets_real_funding_earns_from_then_on():
    faucet, a, other = A(100), A(1), A(2)
    b = [blk(1, miner=faucet), blk(2, xfer(faucet, other, 10 * NLT)),
         blk(3, xfer(other, a, NLT // 2)),                   # dust only: a is unrooted
         blk(FORK + 10, xfer(a, other, NLT // 2))]
    assert a not in c.family_roots(b, issuers=(faucet,))
    b += [blk(4, xfer(other, a, NLT))]                       # a real payment arrives
    assert c.family_roots(b, issuers=(faucet,))[a] == other


# ---- review M3: treasury / reserve grants ----

def test_multisig_spend_grants_do_not_pull_grantees_into_one_family():
    treasury = "e26742c7bcdb96fa0ca9446073b04a35"
    grants = [{"type": "multisig_spend", "payload": {"bucket": "treasury", "recipient": A(500 + i),
                                                      "amount": 5 * NLT, "nonce": i + 1}, "sigs": []}
              for i in range(4)]
    b = [blk(1, miner=treasury), blk(2, *grants)]
    roots = c.family_roots(b)
    assert [roots[A(500 + i)] for i in range(4)] == [A(500 + i) for i in range(4)]    # each its own family


# ---- review H2: release gate and startup refusal ----

def test_release_gate_issuer_list_is_non_empty_and_valid():
    """If this fails the build must not ship: an empty or malformed list silently switches the rule off for ever."""
    assert c.validate_family_issuers() == c.FAMILY_ISSUERS
    assert len(c.FAMILY_ISSUERS) >= 1
    assert all(len(a) == 32 and a == a.lower() and set(a) <= set("0123456789abcdef") for a in c.FAMILY_ISSUERS)


@pytest.mark.parametrize("bad", [(), [], ("zz" * 16,), ("AB" * 16,), ("e924523b",), ("a" * 32, "a" * 32), (None,)])
def test_validate_rejects_empty_malformed_and_duplicate_lists(bad):
    with pytest.raises(c.FamilyIssuerError):
        c.validate_family_issuers(bad)


@pytest.mark.parametrize("bad", [(), ("not-hex",)])
def test_node_refuses_to_start_with_a_bad_issuer_list_only_when_the_family_rule_is_on(monkeypatch, tmp_path, bad):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(c, "FAMILY_ISSUERS", bad)
    with pytest.raises(c.FamilyIssuerError):                 # the autouse flag has the rule ON here
        core.Blockchain()


@pytest.mark.parametrize("bad", [(), ("not-hex",), None])
def test_v4_rule_off_an_empty_or_odd_issuer_list_cannot_stop_the_node_or_change_a_payout(monkeypatch, tmp_path, bad):
    """Points v4 ships with the family rule OFF: the list is then not consensus data at all."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core.Blockchain, "save_chain", lambda self: None)
    monkeypatch.setattr(c, "FAMILY_RULE_ENABLED", False)
    base_hash = core.points_v3_rules_hash()
    monkeypatch.setattr(c, "FAMILY_ISSUERS", bad)
    bc = core.Blockchain()                                   # no refusal
    assert bc.chain
    assert core.points_v3_rules_hash() == base_hash          # handshake fingerprint does not depend on the list
    assert core.Blockchain._v3_args(bc.chain, 5000)["roots"] is None      # fails open to "no family rule", by design


def test_a_valid_list_that_matches_no_funder_still_fails_safe_to_design_a():
    """The two safeguards are different: malformed = refuse to start; well-formed but wrong = no family rule."""
    chain = [blk(1, miner=A(1)), blk(2, xfer(A(1), A(2)))]
    assert c.active_family_roots(chain, issuers=("ab" * 16,)) is None


# ---- review M1 decision: the family rule is off in the shipped build ----

def test_shipped_default_is_family_rule_off(monkeypatch):
    monkeypatch.undo()                                       # drop the autouse flag
    assert c.FAMILY_RULE_ENABLED is False and c.FAMILY_PAIR_CAP is None
    chain = [core.Block(index=0, timestamp=0, transactions=[], previous_hash="0", nonce=0, difficulty=1, hash="0")]
    assert core.Blockchain._v3_args(chain, 5000)["roots"] is None


def test_pair_cap_limits_two_family_pairs():
    hub1, hub2, faucet = A(201), A(202), A(100)
    n = 6
    f1, f2 = [A(300 + i) for i in range(n)], [A(400 + i) for i in range(n)]
    b = [blk(1, miner=faucet), blk(2, miner=hub1), blk(3, miner=hub2),
         blk(4, *[xfer(hub1, a) for a in f1], *[xfer(hub2, a) for a in f2], xfer(faucet, A(900), 10 * NLT)),
         blk(FORK + 5, *[xfer(f1[i], f2[(i + o) % n]) for o in (0, 1, 2) for i in range(n)])]   # 18 cross-family transfers
    roots = c.active_family_roots(b, issuers=(faucet,))
    kw = dict(rules_height=FORK, first_seen=c.first_seen_heights(b), v3=True, roots=roots)
    free = c.points_from_blocks(b, E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                                E.epoch_blocks, epoch_fn=E.epoch_id, **kw)
    capped = c.points_from_blocks(b, E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                                  E.epoch_blocks, epoch_fn=E.epoch_id, family_pair_cap=4, **kw)
    assert sum(free[a]["point_tx_count"] for a in f1) == 18
    assert sum(capped[a]["point_tx_count"] for a in f1) == 4


# ---- review M2: optional ceiling per point ----

def users_with(tx_points_each, mined=0):
    return {a: {"blocks_mined": mined, "empty_blocks": 0, "point_tx_count": n} for a, n in tx_points_each.items()}


def test_ceiling_per_point_is_off_by_default_and_changes_nothing():
    u = users_with({A(1): 1})
    base = c.v3_amounts(u, E.points_per_block, E.points_per_tx, 20_000 * NLT, 30, E.payout_cap)
    assert base == {A(1): 6_000 * NLT}                       # the whole transfer pot to one transfer
    assert c.MAX_BASE_PER_POINT is None


def test_ceiling_per_point_stops_a_quiet_network_paying_its_whole_pot():
    u = users_with({A(1): 1})
    out = c.v3_amounts(u, E.points_per_block, E.points_per_tx, 20_000 * NLT, 30, E.payout_cap, max_per_point=100 * NLT)
    assert out == {A(1): 100 * NLT}


def test_ceiling_per_point_does_not_touch_a_busy_network():
    u = users_with({A(i): 10 for i in range(100)})           # 1000 points; 6,000 NLT pot = 6 NLT a point
    a = c.v3_amounts(u, E.points_per_block, E.points_per_tx, 20_000 * NLT, 30, E.payout_cap)
    b = c.v3_amounts(u, E.points_per_block, E.points_per_tx, 20_000 * NLT, 30, E.payout_cap, max_per_point=100 * NLT)
    assert a == b


# ---- review M4: the points rules are part of the handshake ----

def test_rules_hash_changes_with_every_consensus_constant(monkeypatch):
    base = core.points_v3_rules_hash()
    assert len(base) == 64 and base == core.points_v3_rules_hash()
    changes = [(core, "POINTS_V4_HEIGHT", 6000), (c, "TX_POT_PERCENT", 25), (c, "FAMILY_RULE_ENABLED", False),
               (c, "POT_CAP_PERCENT", 3), (c, "PAYOUT_LAG_PERIODS", 0), (c, "FAMILY_PAIR_CAP", 10), (c, "MAX_BASE_PER_POINT", 1), (c, "FAMILY_ISSUERS", c.FAMILY_ISSUERS[:-1])]
    for mod, attr, val in changes:
        with monkeypatch.context() as m:
            m.setattr(mod, attr, val)
            assert core.points_v3_rules_hash() != base, attr


def test_status_reports_the_rules_hash(node):
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        assert json.loads(r.read())["points_v3_rules"] == core.points_v3_rules_hash()


def _peer_with(rules):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"node": f"http://127.0.0.1:{self.server.server_address[1]}", "chain_id": "netlithic-dev-1",
                    "version": "0.5.0"}
            if rules is not None:
                body["points_v3_rules"] = rules
            b = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_hello_refuses_a_peer_with_different_points_rules(node):
    from conftest import post
    srv = _peer_with("0" * 64)
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert status == 400 and "Points rules mismatch" in body["error"]
        assert not node.peers
    finally:
        srv.shutdown()


@pytest.mark.parametrize("same", [True, None])
def test_hello_accepts_matching_or_unreported_points_rules(node, same):
    from conftest import post
    srv = _peer_with(core.points_v3_rules_hash() if same else None)
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert status == 200
    finally:
        srv.shutdown()


# ---- ops/check_family_issuers.py (read-only release check) ----

def test_issuer_check_script_passes_and_fails_as_documented(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("cfi", os.path.join(ROOT, "ops", "check_family_issuers.py"))
    cfi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfi)
    faucet = c.FAMILY_ISSUERS[0]
    good = [blk(1, miner=faucet), blk(2, xfer(faucet, A(1), 10 * NLT))]
    f = tmp_path / "chain.json"
    f.write_text(json.dumps({"chain": good}))
    assert cfi.main(["x", str(f)]) == 0
    f.write_text(json.dumps([blk(1, miner=A(5)), blk(2, xfer(A(5), A(1), 10 * NLT))]))     # nobody listed funded anyone
    assert cfi.main(["x", str(f)]) == 1
    hub = [blk(1, miner=faucet), blk(2, xfer(faucet, A(9), 10 * NLT)), blk(3, miner=A(7)),
           blk(4, *[xfer(A(7), A(20 + i)) for i in range(3)])]
    f.write_text(json.dumps(hub))
    assert cfi.main(["x", str(f)]) == 2                                                     # unlisted funder warning
    monkeypatch.setattr(c, "FAMILY_ISSUERS", ())
    assert cfi.main(["x", str(f)]) == 1
