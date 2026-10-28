"""Compare points-farming fixes on synthetic chains (real shipped constants). No network, no keys.

    python ops/sim/points_sim.py            # prints the comparison tables
    python ops/sim/points_sim.py --v4       # only part 3: points v3 against points v4
    python ops/sim/points_sim.py --registry # only part 4: points v4 (open) against v4 + registered contributors
    python ops/sim/points_sim.py --mining   # only part 5: what share of the mining pot a miner takes, one address or many

Parts 1 and 2 are the older design comparison (candidates now, A, B, C, C0; the transfer pot is 30% there, the v3
value, kept in V3_TX_POT). Part 3 compares what v3 would pay with what v4 pays, for the same synthetic chains:
  v3        30% transfer pot / 70% mining pot, 25,000 NLT cap per address, family rule OFF
  v3+fam    as v3 with the funding-family rule ON (faucet configured as the issuer)
  v4        20% / 80% pots, 2% of each pot per address, family rule OFF (the shipped v4 settings)
The one-day payout lag changes WHEN a period is paid, not how much, so it does not appear in these amounts.
Part 4 (registry) compares v4 with v4 + the registered-contributors option (TEMPORARY, CENTRALIZED):
only SENDERS on the list earn transfer points, mining points stay open. The list is applied in
the simulator as "this set of senders is listed for the whole period" (the one-day delay has passed; the real delay and
removal rules are tested in tests/test_registry.py).
Part 5 (mining) is the honest answer to "does the 2% cap protect the mining pot?": it does not. The mining pot follows
BLOCK SHARE, and one miner who spreads their blocks over enough addresses takes their whole block share, however
big. The only thing that limits it is hash power. Part 5 also shows the empty-block discount that points v4 removes.
Every number comes from running the shipped functions in contributions.py on the generated chain.
Test coins have no value.
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import contributions as c
import economics as eco

NLT = eco.BASE_UNITS_PER_NLT
E = eco.load_economics()
FORK, PERIOD = 5000, 1440
POOL = 2_500_000 * NLT                                   # first month-step, fully available at 6440
BUDGET = POOL * PERIOD // E.blocks_per_month             # ~20,548 NLT per period under A/B/C
FEE = 10_000
V3_TX_POT = 30                                           # the v3 transfer pot; v4 uses V3_TX_POT (20)


def addr(tag, i):
    return hashlib.sha256(f"{tag}-{i}".encode()).hexdigest()[:32]


def xfer(s, r, amt=NLT):
    return {"payload": {"sender": s, "recipient": r, "amount": amt, "nonce": 1, "fee": FEE}}


def reward(a):
    return {"type": "reward", "recipient": a, "amount": 0, "fees": 0}


def build(n_fake=0, n_honest=0, fake_funding="hub", offsets=(1, 2, 3), per_offset=10, n_miners=3,
          honest_tx=3, friend_pairs=0):
    """Blocks 0..6439 as dicts. Returns (blocks, actors)."""
    F, HUB = addr("faucet", 0), addr("hub", 0)
    fakes = [addr("fake", i) for i in range(n_fake)]
    honest = [addr("honest", i) for i in range(n_honest)]
    merch = [addr("merchant", i) for i in range(honest_tx)]
    miners = [addr("miner", i) for i in range(n_miners)]
    setup = []                                           # (rewards, transfers) placed in blocks 1..
    rewards = [F, HUB] + (fakes if fake_funding == "mined" else [])
    transfers = []
    if fake_funding == "hub":
        transfers += [xfer(HUB, f) for f in fakes]
    elif fake_funding == "faucet":
        transfers += [xfer(F, f) for f in fakes]
    transfers += [xfer(F, a) for a in honest + merch]
    blocks = [{"index": 0, "transactions": []}]
    for i in range(1, FORK):
        txs = []
        if i <= len(rewards):
            txs.append(reward(rewards[i - 1]))
        else:
            txs.append(reward(miners[i % n_miners]))
        if 100 <= i < 100 + len(transfers):
            txs.append(xfer_copy(transfers[i - 100]))
        blocks.append({"index": i, "transactions": txs})
    seq = []
    if n_fake:
        ring = fakes
        n = len(ring)
        seq += [(ring[i], ring[(i + o) % n]) for o in offsets for _ in range(per_offset) for i in range(n)]
    seq += [(h, m) for h in honest for m in merch[:honest_tx]]
    for i in range(FORK, FORK + PERIOD):
        blocks.append({"index": i, "transactions": [reward(miners[i % n_miners])]})
    for j, (s, r) in enumerate(seq):
        blocks[FORK + (j * PERIOD) // len(seq)]["transactions"].append(xfer(s, r))
    return blocks, dict(fakes=fakes, honest=honest, miners=miners, faucet=F, hub=HUB, txs=len(seq))


def xfer_copy(t):
    return {"payload": dict(t["payload"])}


def points(blocks, mode, issuers=()):
    seen = c.first_seen_heights(blocks)
    window = [b for b in blocks if FORK <= b["index"] < FORK + PERIOD]
    kw = dict(rules_height=FORK, first_seen=seen)
    if mode in ("C", "C0"):
        kw.update(v3=True, roots=c.family_roots(blocks, issuers=issuers))
    if mode == "B":
        return points_diminishing(window, seen)
    return c.points_from_blocks(window, E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id, **kw)


# B: a sender's transfer points per period: 1st-5th distinct recipient 1.0, 6th-15th 0.5, 16th-30th 0.25,
# repeats to the same recipient earn nothing. (Only the weights change; other v2 rules stay.)
B_WEIGHTS = [10] * 5 + [5] * 10 + [2.5] * 15


def points_diminishing(window, seen):
    base = c.points_from_blocks(window, E.points_per_block, E.points_per_tx, 0, 10 ** 9, E.epoch_blocks,
                                epoch_fn=E.epoch_id)           # only for mining points / bookkeeping
    users = {a: {"blocks_mined": u["blocks_mined"], "empty_blocks": u["empty_blocks"], "point_tx_count": 0,
                 "tx_tenths": 0} for a, u in base.items()}
    partners, earned = {}, set()
    for b in window:
        for tx in b["transactions"]:
            if "payload" not in tx or "type" in tx:
                continue
            p = tx["payload"]
            s, r = p["sender"], p["recipient"]
            if s == r or p["amount"] < E.min_point_transfer or b["index"] - seen.get(s, b["index"]) < c.MIN_ADDRESS_AGE_BLOCKS:
                continue
            ps = partners.setdefault(s, [])
            if r in ps or (r, s) in earned:
                continue
            if len(ps) >= len(B_WEIGHTS):
                continue
            earned.add((s, r))
            users[s]["tx_tenths"] += B_WEIGHTS[len(ps)]
            ps.append(r)
    for a, u in users.items():
        u["tx_tenths"] = int(u["tx_tenths"])
        u["point_tx_count"] = u["tx_tenths"] // E.points_per_tx            # whole points only for the pot split
        u["cp_tenths"] = u["tx_tenths"] + (u["blocks_mined"] - u["empty_blocks"]) * E.points_per_block
    return users


def payouts(users, mode):
    if mode == "now":
        cp = {a: u["cp_tenths"] for a, u in users.items() if u["cp_tenths"] > 0}
        tot = sum(cp.values())
        return {a: min(POOL * v // tot, E.payout_cap) for a, v in cp.items() if POOL * v // tot > 0}
    if mode == "B":
        # B's fractional weights: use tx_tenths directly
        us = {a: dict(u, point_tx_count=u["tx_tenths"] // E.points_per_tx) for a, u in users.items()}
        mine = {a: (u["blocks_mined"] - u["empty_blocks"]) * E.points_per_block for a, u in us.items()}
        tx = {a: u["tx_tenths"] for a, u in us.items()}
        txb = BUDGET * V3_TX_POT // 100
        out = {}
        tm, tt = sum(mine.values()), sum(tx.values())
        for a in set(mine) | set(tx):
            amt = (txb * tx.get(a, 0) // tt if tt else 0) + ((BUDGET - txb) * mine.get(a, 0) // tm if tm else 0)
            if amt:
                out[a] = min(amt, E.payout_cap)
        return out
    return c.v3_amounts(users, E.points_per_block, E.points_per_tx, BUDGET, V3_TX_POT, E.payout_cap)


def run(label, mode, world_kwargs, issuer_aware):
    blocks, w = build(**world_kwargs)
    issuers = (w["faucet"],) if issuer_aware else ()
    users = points(blocks, mode if mode != "B" else "B", issuers)
    pay = payouts(users, mode)
    nl = lambda x: x / NLT
    fake_pay = sum(pay.get(a, 0) for a in w["fakes"])
    honest_pay = sum(pay.get(a, 0) for a in w["honest"])
    paid = sum(pay.values())
    tx_pts_fake = sum(users.get(a, {}).get("point_tx_count", 0) for a in w["fakes"])
    hp = users.get(w["honest"][0], {}).get("point_tx_count", 0) if w["honest"] else None
    h1 = pay.get(w["honest"][0], 0) if w["honest"] else None
    return dict(label=label, fake_share=(fake_pay / paid if paid else 0), fake_nlt=nl(fake_pay), paid=nl(paid),
                honest_nlt=nl(honest_pay), one_honest_nlt=(nl(h1) if h1 is not None else None), one_honest_pts=hp,
                fake_tx_pts=tx_pts_fake, txs=w["txs"])


CANDIDATES = [("now (shipped)", "now", False), ("A slow release + pots", "A", False),
              ("B few good friends", "B", False), ("C families (faucet set)", "C", True),
              ("C0 families (faucet NOT set)", "C0", False)]
SCENARIOS = [
    ("400 fakes (one hub)", dict(n_fake=400, n_honest=0, fake_funding="hub")),
    ("120 fakes + 20 honest", dict(n_fake=120, n_honest=20, fake_funding="hub")),
    ("7 honest + 3 miners", dict(n_fake=0, n_honest=7)),
    ("72 fakes, each mined-born + 20 honest", dict(n_fake=72, n_honest=20, fake_funding="mined")),
    ("50 fakes, faucet-born + 20 honest", dict(n_fake=50, n_honest=20, fake_funding="faucet")),
]


# ---------------------------------------------------------------------------------------------
# Part 2 (review H1, M1, M2): what each family rule does against smarter farms, and to honest users.
# Rules compared on the same chains (all use the same two pots and slow release):
#   A only            no family rule
#   C v1 (40dada8)    the first draft: family root = first >= 1 NLT funder, even if that funder has no family
#   C hardened        running-total rooting, unrooted senders earn nothing, multisig grants rooted
#   C + pair cap 10   hardened, plus at most 10 transfer points per (sender family, recipient family) pair
# ---------------------------------------------------------------------------------------------

def roots_v1(blocks, issuers, min_amount=NLT):
    """The first draft (commit 40dada8), kept only so the tables can show what the review found."""
    root, seen = {}, set()
    for b in blocks:
        for tx in b.get("transactions") or []:
            if "payload" in tx and "type" not in tx:
                p = tx["payload"]
                r, s_ = p["recipient"], p["sender"]
                if r in root or s_ == r or p["amount"] < min_amount:
                    continue
                root[r] = r if s_ in issuers else root.get(s_, s_)
            elif tx.get("recipient") and tx["recipient"] not in root:
                root[tx["recipient"]] = tx["recipient"]
    return root


RULES = ["A only", "C v1 (first draft)", "C hardened", "C + pair cap 10"]


def world(rewards, setup, seq, honest=20, merchants=3, honest_seq=None):
    """Blocks 0..6439. `setup` transfers go in early blocks, `seq` (farm) and honest payments spread over the period."""
    F = addr("faucet", 0)
    hon = [addr("hon", i) for i in range(honest)]
    merch = [addr("merch", i) for i in range(merchants)]
    miners = [addr("m", i) for i in range(3)]
    early = list(rewards) + [F] + hon + merch
    setup = [xfer(F, a, 10 * NLT) for a in hon + merch] + list(setup)
    blocks = [{"index": 0, "transactions": []}]
    for i in range(1, FORK):
        txs = [reward(early[i - 1] if i <= len(early) else miners[i % 3])]
        k = i - 100
        if 0 <= k < len(setup):
            txs.append(xfer_copy(setup[k]))
        blocks.append({"index": i, "transactions": txs})
    live = list(seq) + (honest_seq if honest_seq is not None else [(h, m) for h in hon for m in merch])
    for i in range(FORK, FORK + PERIOD):
        blocks.append({"index": i, "transactions": [reward(miners[i % 3])]})
    for j, (s_, r) in enumerate(live):
        blocks[FORK + (j * PERIOD) // len(live)]["transactions"].append(xfer(s_, r))
    return blocks, F, hon, merch


def score(blocks, F, rule, who, honest, max_per_point=None):
    seen = c.first_seen_heights(blocks)
    window = [b for b in blocks if FORK <= b["index"] < FORK + PERIOD]
    kw = dict(rules_height=FORK, first_seen=seen, v3=True)
    if rule == "C v1 (first draft)":
        kw["roots"] = roots_v1(blocks, (F,))
        # the first draft had no "unrooted" notion: unknown addresses count as their own family
        kw["roots"] = _Lenient(kw["roots"])
    elif rule in ("C hardened", "C + pair cap 10"):
        kw["roots"] = c.active_family_roots(blocks, issuers=(F,))
        if rule.endswith("10"):
            kw["family_pair_cap"] = 10
    users = c.points_from_blocks(window, E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                 E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id, **kw)
    pay = c.v3_amounts(users, E.points_per_block, E.points_per_tx, BUDGET, V3_TX_POT, E.payout_cap,
                       max_per_point=max_per_point)
    paid = sum(pay.values())
    fk = sum(pay.get(a, 0) for a in who)
    hp = sum(users.get(a, {}).get("point_tx_count", 0) for a in honest)
    return fk / NLT, (fk / paid if paid else 0), paid / NLT, hp, sum(pay.get(a, 0) for a in honest) / NLT


class _Lenient(dict):
    """roots.get / [] / in behave like the first draft: an address with no entry is its own family."""
    def __contains__(self, k):
        return True

    def __getitem__(self, k):
        return self.get(k, k)


def ring(nodes, k=1, per=10):
    offs = [o for o in range(1, 40) if k == 1 or o % k][:3]
    n = len(nodes)
    return [(nodes[i], nodes[(i + o) % n]) for o in offs for _ in range(per) for i in range(n)]


def attack_worlds(n=120):
    fk = [addr("fk", i) for i in range(n)]
    out = {}
    hub = addr("hub", 0)
    out["hub ring (1 root)"] = (world([hub], [xfer(hub, f) for f in fk], ring(fk)), fk)
    xs = [addr("x", i) for i in range(n)]
    st = []
    for x in xs:
        st += [xfer(hub, x, 60_000_000), xfer(hub, x, 60_000_000)]
    st += [xfer(x, f) for x, f in zip(xs, fk)]
    out["H1 dust-staged (1 hub)"] = (world([hub], st, ring(fk)), fk)
    for k in (2, 12, 60):
        hubs = [addr("hubk", g) for g in range(k)]
        st = [xfer(hubs[i % k], f) for i, f in enumerate(fk)]
        out[f"M1 {k} roots (mined blocks)"] = (world(hubs, st, ring(fk, k)), fk)
    return out


def exchange_world(customers=50, merchants=3):
    """Honest harm: an exchange W (funded by the faucet, so its own family) withdraws coins to 50 new customers,
    who pay 3 shops (each its own family) and a few friends among the customers."""
    W = addr("exch", 0)
    cust = [addr("cust", i) for i in range(customers)]
    shops = [addr("shop", i) for i in range(merchants)]
    F = addr("faucet", 0)
    st = [xfer(F, W, 100 * NLT)] + [xfer(W, a, 5 * NLT) for a in cust]
    seq = [(a, s_) for a in cust for s_ in shops] + [(cust[i], cust[(i + 1) % customers]) for i in range(customers)]
    b, F, hon, merch = world([], st, [], honest=0, merchants=0, honest_seq=seq)
    # shops need roots: give them faucet drips by reusing world() with merchants listed as extra honest
    return b, F, cust, shops


def attack_tables():
    out = ["\n# Part 2: smarter farms and honest harm (120 fakes unless noted; 20 honest users pay 3 shops each)",
           f"{'attack':30} {'rule':20} {'fakes get':>12} {'share':>6} {'total paid':>11} {'honest tx pts':>13} {'honest NLT':>11}"]
    for name, ((blocks, F, hon, merch), fk) in attack_worlds().items():
        for rule in RULES:
            f_nlt, share, paid, hp, h_nlt = score(blocks, F, rule, fk, hon)
            out.append(f"{name:30} {rule:20} {f_nlt:>9,.0f}NLT {share:>6.0%} {paid:>11,.0f} {hp:>13} {h_nlt:>11,.0f}")
        out.append("")
    # honest harm: exchange customers
    W, F = addr("exch", 0), addr("faucet", 0)
    cust = [addr("cust", i) for i in range(50)]
    shops = [addr("shop", i) for i in range(3)]
    st = [xfer(F, s_, 10 * NLT) for s_ in shops] + [xfer(F, W, 400 * NLT)] + [xfer(W, a, 5 * NLT) for a in cust]
    seq = [(a, s_) for a in cust for s_ in shops] + [(cust[i], cust[(i + 1) % 50]) for i in range(50)]
    blocks, F2, _, _ = world([], st, [], honest=0, merchants=0, honest_seq=seq)
    out.append("honest harm: exchange withdraws to 50 customers; each pays 3 shops and 1 friend (200 transfers)")
    for rule in RULES:
        _, _, paid, hp, h_nlt = score(blocks, F2, rule, [], cust)
        out.append(f"{'50 exchange customers':30} {rule:20} {'':>12} {'':>6} {paid:>11,.0f} {hp:>13} {h_nlt:>11,.0f}")
    # M2: quiet network, with and without a ceiling per point
    out.append("\nM2 quiet network: one honest user makes ONE transfer, two miners mine; transfer pot is 30% (the v3 value) of the day's 20,548 NLT")
    hon = [addr("hon", 0)]
    blocks, F3, hon, merch = world([], [], [], honest=1, merchants=1, honest_seq=[(hon[0], addr("merch", 0))])
    for mpp in (None, 250 * NLT, 100 * NLT, 50 * NLT):
        _, _, paid, hp, h_nlt = score(blocks, F3, "C hardened", [], hon, max_per_point=mpp)
        lab = "no ceiling" if mpp is None else f"ceiling {mpp // NLT} NLT per point"
        out.append(f"  {lab:28} that one transfer earns {h_nlt:>8,.0f} NLT; total paid {paid:>8,.0f} NLT (rest stays in the pool)")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# Part 3: points v3 against points v4 on the same synthetic chains.
# ---------------------------------------------------------------------------------------------

VARIANTS = [("v3 (30/70, 25,000 cap)", "v3"), ("v3 + family rule on", "v3fam"), ("v4 (20/80, 2% cap)", "v4")]


def amounts_for(blocks, F, variant, listed=None):
    """variant: v3 | v3fam | v4 | v4reg (v4 + registered senders `listed`; an empty or missing list pays no transfers)."""
    seen = c.first_seen_heights(blocks)
    window = [b for b in blocks if FORK <= b["index"] < FORK + PERIOD]
    kw = dict(rules_height=FORK, first_seen=seen, v3=True)
    if variant == "v3fam":
        kw["roots"] = c.active_family_roots(blocks, issuers=(F,))
        kw["family_pair_cap"] = c.FAMILY_PAIR_CAP
    if variant in ("v4", "v4reg"):
        kw["flat_block_points_height"] = FORK            # review H1: every block earns the block points from the fork
    if variant == "v4reg":
        ok = frozenset(listed or ())
        kw["registry"] = lambda sender, height: sender in ok
    users = c.points_from_blocks(window, E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                 E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id, **kw)
    if variant in ("v4", "v4reg"):
        return c.v3_amounts(users, E.points_per_block, E.points_per_tx, BUDGET, c.TX_POT_PERCENT, E.payout_cap,
                            pot_cap_percent=c.POT_CAP_PERCENT)
    return c.v3_amounts(users, E.points_per_block, E.points_per_tx, BUDGET, V3_TX_POT, E.payout_cap)


def v4_worlds():
    """name -> (blocks, faucet, fakes, honest). 3 miners mine all blocks of the window in every world."""
    hub = addr("hub", 0)
    fk = [addr("fk", i) for i in range(400)]
    out = {}
    b, F, hon, _ = world([hub], [xfer(hub, f) for f in fk], ring(fk), honest=20)
    out["400-fake ring funded by one hub, 20 honest users"] = (b, F, fk, hon)
    b, F, hon, _ = world(fk, [], ring(fk), honest=20)
    out["400 mining-born fakes (each mined 1 block), 20 honest users"] = (b, F, fk, hon)
    b, F, hon, _ = world([], [], [], honest=20)
    out["no attack: 20 honest users"] = (b, F, [], hon)
    b, F, hon, _ = world([], [], [], honest=200)
    out["no attack: 200 honest users"] = (b, F, [], hon)
    return out


def v4_tables():
    miners = {addr("m", i) for i in range(3)}
    out = [f"\n# Part 3: points v3 against points v4 (one period, release budget {BUDGET / NLT:,.0f} NLT; "
           f"v4 per-address limit {c.POT_CAP_PERCENT}% of each pot)",
           f"{'world':62} {'rules':24} {'fakes get':>12} {'fake share':>10} {'honest users':>13} {'3 miners':>10} {'paid':>9} {'of budget':>9}"]
    results = {}
    for name, (blocks, F, fk, hon) in v4_worlds().items():
        for label, var in VARIANTS:
            pay = amounts_for(blocks, F, var)
            tot = sum(pay.values())
            f = sum(pay.get(a, 0) for a in fk)
            h = sum(pay.get(a, 0) for a in hon)
            m = sum(pay.get(a, 0) for a in miners)
            results[(name, var)] = dict(fakes=f / NLT, honest=h / NLT, miners=m / NLT, paid=tot / NLT,
                                        share=(f / tot if tot else 0), budget_share=tot / BUDGET)
            out.append(f"{name:62} {label:24} {f / NLT:>9,.0f}NLT {(f / tot if tot else 0):>10.0%} {h / NLT:>10,.0f}NLT"
                       f" {m / NLT:>7,.0f}NLT {tot / NLT:>9,.0f} {tot / BUDGET:>9.0%}")
        out.append("")
    out.append("Reading the table: 'fakes get' is what the fake addresses take of ONE period's release; 'of budget' is how much of the")
    out.append("period's release is paid at all (the rest stays in the pool, nothing is burned or lost).")
    return "\n".join(out), results


# ---------------------------------------------------------------------------------------------------------------
# Part 4: v4 (open transfer pot) against v4 + registered contributors
# ---------------------------------------------------------------------------------------------------------------
def mine_in_window(blocks, addrs, first_offset=0):
    """Hand the reward of window blocks FORK+first_offset, +1, ... to `addrs` (one block each). Those blocks already carry
    other people's transfers, so they count as real (non-empty) mined blocks. Returns the same list."""
    for k, a_ in enumerate(addrs):
        blocks[FORK + first_offset + k]["transactions"][0] = reward(a_)
    return blocks


def reg_worlds():
    """name -> dict(blocks, F, fakes, honest, listed, window_miners). Every world has 3 honest miners unless stated."""
    hub = addr("hub", 0)
    fk = [addr("fk", i) for i in range(400)]
    out = {}

    def add(name, blocks, F, fakes, honest, listed, window_miners=()):
        out[name] = dict(blocks=blocks, F=F, fakes=fakes, honest=honest, listed=set(listed), window_miners=set(window_miners))

    b, F, hon, _ = world([hub], [xfer(hub, f) for f in fk], ring(fk), honest=20)
    add("A. 400-fake ring (hub-funded); 20 honest users, all listed", b, F, fk, hon, hon)
    b, F, hon, _ = world(fk, [], ring(fk), honest=20)
    add("B. 400 mining-born fakes (mined before the window); 20 honest listed", b, F, fk, hon, hon)
    b, F, hon, _ = world([], [], [], honest=20)
    add("C. no attack: 20 honest users, all listed", b, F, [], hon, hon)
    b, F, hon, _ = world([], [], [], honest=20)
    add("D. no attack: 20 honest users, only 10 listed (ten testers)", b, F, [], hon, hon[:10])
    b, F, hon, _ = world([], [], [], honest=20)
    add("E. no attack: 20 honest users, NOBODY listed (empty list)", b, F, [], hon, [])
    # F: the attacker also has real hash power: 10% of the window's blocks (144), each to a different fake address
    b, F, hon, _ = world([hub], [xfer(hub, f) for f in fk], ring(fk), honest=20)
    mine_in_window(b, fk[:144])
    add("F. ring + fakes mine 10% of the window's blocks (144 blocks, 1 each); honest listed", b, F, fk, hon, hon)
    # G: same hash power on ONE address (what an honest big miner would look like)
    b, F, hon, _ = world([hub], [xfer(hub, f) for f in fk], ring(fk), honest=20)
    one = [fk[0]] * 144
    mine_in_window(b, one)
    add("G. same 10% hash power on ONE address (no splitting); ring unlisted", b, F, fk, hon, hon)
    # H/I: bad registrations: the key (or a fooled reviewer) lists fakes. 50 = one period's maximum, 10 = a small slip
    for n in (10, 50):
        b, F, hon, _ = world([hub], [xfer(hub, f) for f in fk], ring(fk), honest=20)
        add(f"{'H' if n == 10 else 'I'}. ring of 400, but {n} of the fakes got listed; 20 honest listed", b, F, fk, hon, list(hon) + fk[:n])
    return out


REG_VARIANTS = [("v4 (open transfer pot)", "v4"), ("v4 + registry", "v4reg")]


def reg_tables():
    miners = {addr("m", i) for i in range(3)}
    head = (f"\n# Part 4: points v4 (open transfer pot) against v4 + registered contributors (TEMPORARY, CENTRALIZED)\n"
            f"# one period, release budget {BUDGET / NLT:,.0f} NLT = {BUDGET * c.TX_POT_PERCENT // 100 / NLT:,.0f} transfer pot + "
            f"{BUDGET * (100 - c.TX_POT_PERCENT) // 100 / NLT:,.0f} mining pot; per-address limit {c.POT_CAP_PERCENT}% of each pot\n"
            f"{'world':78} {'rules':23} {'fakes get':>10} {'fake share':>10} {'honest':>9} {'3 miners':>9} {'paid':>8} {'of budget':>9}")
    out, results = [head], {}
    for name, w in reg_worlds().items():
        for label, var in REG_VARIANTS:
            pay = amounts_for(w["blocks"], w["F"], var, listed=w["listed"])
            tot = sum(pay.values())
            f = sum(pay.get(a_, 0) for a_ in w["fakes"])
            h = sum(pay.get(a_, 0) for a_ in w["honest"])
            m = sum(pay.get(a_, 0) for a_ in miners)
            results[(name, var)] = dict(fakes=f / NLT, honest=h / NLT, miners=m / NLT, paid=tot / NLT,
                                        share=(f / tot if tot else 0), budget_share=tot / BUDGET)
            out.append(f"{name:78} {label:23} {f / NLT:>7,.0f}NLT {(f / tot if tot else 0):>10.0%} {h / NLT:>6,.0f}NLT"
                       f" {m / NLT:>6,.0f}NLT {tot / NLT:>8,.0f} {tot / BUDGET:>9.0%}")
        out.append("")
    out += ["Reading the table: 'fakes get' is what the fake addresses take of ONE period's release; 'of budget' is how much of the",
            "period's release is paid at all (the rest stays in the pool, nothing is burned or lost).",
            "World F/G: mining points are open to everyone, so fake addresses that really mine blocks still earn from the mining pot,",
            "in proportion to the blocks they mined (that needs real hash power). The registry does not change that.",
            "World H/I: the registry is only as good as the review. Listed fakes earn like anybody else, up to the per-address limit."]
    return "\n".join(out), results


# ---------------------------------------------------------------------------------------------------------------
# Part 5: the mining pot follows block share. One miner, one or many addresses, at 5 / 30 / 60 / 90% of the blocks.
# ---------------------------------------------------------------------------------------------------------------
MINING_POT = BUDGET - BUDGET * c.TX_POT_PERCENT // 100


def mining_run(share_pct, k_addr, flat=True, evil_dust=True, honest_busy=True, n_other=3):
    """One miner with `share_pct`% of the blocks, spread round-robin over `k_addr` addresses; `n_other` honest single-address
    miners share the rest. evil_dust: the big miner adds ONE 1-base-unit self-staging transfer to every block it mines
    (below the 1 NLT minimum, so it earns no transfer points, but under the OLD rule it made the block 'non-empty').
    honest_busy: honest blocks carry an ordinary transfer; False = a quiet testnet where honest blocks are empty.
    flat=True: the shipped fix (every block earns block points). flat=False: the old empty-block discount.
    Returns (miner NLT, honest NLT, miner share of the mining pot, total paid NLT)."""
    blocks = [{"index": i, "transactions": [reward(addr("early", i))]} for i in range(FORK)]
    mine = [addr("evil", i) for i in range(k_addr)]
    honest = [addr("hm", i) for i in range(n_other)]
    nm = 0
    for i in range(FORK, FORK + PERIOD):
        evil = (i - FORK) % 100 < share_pct
        if evil:
            r = mine[nm % k_addr]
            nm += 1
        else:
            r = honest[i % n_other]
        txs = [reward(r)]
        if evil and evil_dust:
            txs.append(xfer(addr("evil", "bank"), addr("evil", "bank2"), 1))
        elif not evil and honest_busy:
            txs.append(xfer(addr("user", i), addr("shop", 0), NLT))
        blocks.append({"index": i, "transactions": txs})
    seen = c.first_seen_heights(blocks)
    window = [b for b in blocks if FORK <= b["index"] < FORK + PERIOD]
    kw = dict(rules_height=FORK, first_seen=seen, v3=True)
    if flat:
        kw["flat_block_points_height"] = FORK
    users = c.points_from_blocks(window, E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                                 E.epoch_blocks, epoch_fn=E.epoch_id, **kw)
    pay = c.v3_amounts(users, E.points_per_block, E.points_per_tx, BUDGET, c.TX_POT_PERCENT, E.payout_cap,
                       pot_cap_percent=c.POT_CAP_PERCENT)
    ev = sum(pay.get(a_, 0) for a_ in mine)
    ho = sum(pay.get(a_, 0) for a_ in honest)
    return ev / NLT, ho / NLT, ev / MINING_POT, sum(pay.values()) / NLT


SHARES = (5, 30, 60, 90)
ADDRESS_COUNTS = (1, 3, 10, 25, 50, 100)


def mining_tables():
    cap = MINING_POT * c.POT_CAP_PERCENT // 100 / NLT
    out = [f"\n# Part 5: who takes the mining pot? (one period; mining pot {MINING_POT / NLT:,.0f} NLT; per-address cap "
           f"{c.POT_CAP_PERCENT}% = {cap:,.0f} NLT; blocks in the period {PERIOD})",
           "# One miner with the given share of the blocks, spread over k addresses. 3 honest single-address miners share the rest.",
           "# The mining pot follows BLOCK SHARE: the 2% cap holds one address, it does not hold a miner who uses many addresses.",
           f"{'miner hash share':>16} {'addresses':>9} {'miner gets':>11} {'% of mining pot':>15} {'3 honest miners':>16} {'paid % budget':>13}"]
    results = {}
    for share in SHARES:
        for k in ADDRESS_COUNTS:
            ev, ho, frac, paid = mining_run(share, k)
            results[("busy", share, k)] = dict(miner=ev, honest=ho, pot_share=frac, paid=paid)
            out.append(f"{share:>15}% {k:>9} {ev:>8,.0f}NLT {frac:>15.1%} {ho:>13,.0f}NLT {paid / (BUDGET / NLT):>13.0%}")
        out.append("")
    out.append("Reading the table: once the miner uses enough addresses (about share / 2 of them, since each address is held at 2% of the pot),")
    out.append("they take about their block share of the pot. 90% of the hash power takes about 90% of the mining pot. The split into")
    out.append("two pots does not protect the mining pot; the registry only guards the transfer pot.")
    out.append("")
    out.append("QUIET CHAIN (honest miners' blocks carry no transfers, as on a small testnet): a 5% miner who adds one 1-base-unit self-transfer")
    out.append("to each of its blocks. Old rule = empty-block discount (an empty self-mined block earned nothing); fixed rule = every block counts.")
    out.append(f"{'addresses':>9} {'rule':>14} {'miner gets':>11} {'% of mining pot':>15} {'3 honest miners':>16}")
    for k in (1, 10, 50):
        for label, flat in (("old (discount)", False), ("fixed (flat)", True)):
            ev, ho, frac, paid = mining_run(5, k, flat=flat, honest_busy=False)
            results[("quiet", label, k)] = dict(miner=ev, honest=ho, pot_share=frac, paid=paid)
            out.append(f"{k:>9} {label:>14} {ev:>8,.0f}NLT {frac:>15.1%} {ho:>13,.0f}NLT")
    ev, ho, frac, paid = mining_run(5, 50, flat=False, evil_dust=False, honest_busy=False)
    results[("quiet", "control", 50)] = dict(miner=ev, honest=ho, pot_share=frac, paid=paid)
    out.append(f"control: the same 5% miner on 50 addresses WITHOUT the dust transfer, old rule: gets {ev:,.0f} NLT (the discount did apply)")
    out.append("With the fix a 5% miner gets about 5% of the pot whether it adds the dust transfer or not: the discount no longer pays anything.")
    return "\n".join(out), results


def main():
    if "--mining" in sys.argv:
        print(mining_tables()[0])
        return
    if "--v4" in sys.argv:
        print(v4_tables()[0])
        return
    if "--registry" in sys.argv:
        print(reg_tables()[0])
        return
    out = []
    for sname, kw in SCENARIOS:
        out.append(f"\n## {sname}")
        out.append(f"{'candidate':32} {'fakes get':>14} {'share of paid':>13} {'total paid':>12} {'1 honest user':>14}")
        for cname, mode, aware in CANDIDATES:
            kw2 = dict(kw)
            if mode == "B" and kw2.get("n_fake"):
                kw2.update(offsets=tuple(range(1, 31)), per_offset=1)     # attacker adapts: 30 distinct partners
            r = run(cname, mode, kw2, aware)
            h = "-" if r["one_honest_nlt"] is None else f"{r['one_honest_nlt']:.1f} NLT/{r['one_honest_pts']}pt"
            out.append(f"{cname:32} {r['fake_nlt']:>12,.0f}NLT {r['fake_share']:>12.0%} {r['paid']:>11,.0f} {h:>14}")
    print("\n".join(out))
    print(f"\nattacker cost per day, hub ring of 400 x 30 transfers: {400 * 30 * FEE / NLT:,.2f} NLT in fees (no hash power, no stake)")
    print("an unrelated funder (a root) costs one mined block or one faucet drip (<= 50 a day); a farm needs only a few roots, see part 2")
    print(f"\nbudget per period under A/B/C: {BUDGET / NLT:,.0f} NLT; pool 2,500,000 NLT; transfer pot {V3_TX_POT}% (v3 value)")
    print(attack_tables())
    print(v4_tables()[0])
    print(reg_tables()[0])
    print(mining_tables()[0])


if __name__ == "__main__":
    main()
