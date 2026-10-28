#!/usr/bin/env python3
"""Check a payout period's proposed payouts, in plain words. Public and read-only.

    python ops/verify_payouts.py --node http://127.0.0.1:8000 [--period N] [--quick] [--json]

It only ever sends GET requests to the node you name; it writes nothing and needs no keys. What it does:
  1. downloads the chain (/chain) and the node's public rules fingerprint (/status). A missing fingerprint is an ERROR:
     without it we cannot tell which payout rules the node runs, so the check stops (exit 2);
  2. CHECKS THE CHAIN ITSELF before trusting a single block of it. THE DEFAULT IS THE COMPLETE CHECK: the first block
     must equal the genesis block rebuilt from config/genesis_config.json, every shipped checkpoint must match, and the
     node's whole validator runs on every block (links, hashes, difficulty rules and proof of work, balances, rewards,
     nonces, signatures, payout blocks). A chain that fails is refused (exit 2): a dishonest node could otherwise feed
     you a coherent made-up history. It is slower on a long chain.
     --quick is a fast, WEAKER mode and says so on every run: genesis and checkpoint match, links, hashes, a hash that
     meets the block's OWN declared difficulty, and transfer signatures. It does NOT prove the work was real, and it
     can never print "Nothing odd found" (it always ends with a warning and exit code 1);
  3. RECOMPUTES the period's payout list with an INDEPENDENT REIMPLEMENTATION of the published payout math (written
     separately in this file; it does not call the node's payout code) and compares it with
       (a) the payout transactions the chain ACTUALLY paid at the paying block, when that block exists,
       (b) the node's own /payout_preview, and
       (c) the node's consensus code run locally;
  4. flags anything odd: an address over its share limit, a few addresses taking most of a pot, many addresses
     sitting exactly at the transfer limit (what a ring of fake addresses looks like), or a pot that is mostly unspent.

WHAT IS AND IS NOT INDEPENDENT (printed with every run, see LIMITS): the points, pots, caps and budget arithmetic are
reimplemented here. These pieces are shared with the node's code and are NOT independent: the registry list (read with
registry.scan), the economics settings (config/genesis_config.json), the signature primitive, and (unless --quick) the
chain validator. This tool cannot see a multi-address miner: the mining pot follows block share.

Exit code: 0 = nothing odd, 1 = something to look at, 2 = could not check (including a chain that fails its checks).
Test coins have no value. This tool says nothing about price or returns.
"""
import argparse
import json
import os
import sys
import tempfile
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOP_N = 5
TOP_SHARE_WARN = 25      # percent of all paid that the top 5 addresses may take before we say so
SENDER_LIMIT_POINTS = 30  # a sender earns transfer points for at most 30 transfers a period
RING_AT_LIMIT_WARN = 20  # this many addresses at the 30-transfer sender limit looks like a ring
UNSPENT_WARN = 50        # percent of the day's release left in the pool

# ---- the published payout rules, written out here on purpose (INDEPENDENT REIMPLEMENTATION, see below) ----
# tests/test_verify_payouts.py checks every one of these against the consensus constants, so a drift fails the test.
R_FORK = 5000                # points v4 / security fork height
R_TX_POT_PERCENT = 20        # transfer pot share; the rest is the mining pot
R_POT_CAP_PERCENT = 2        # one address takes at most this % of EACH pot
R_PAIR_CAP = 10              # qualifying transfers per (sender, recipient) per period
R_SENDER_CAP = 30            # qualifying transfers per sender per period
R_MIN_AGE_BLOCKS = 1440      # a sender must have first received coins this many blocks before the transfer
R_LAG_PERIODS = 1            # the period that ended one period ago is paid
LIMITS = [
    "Independent: the points, the two pots, the 2% caps, the payout cap and the release-budget arithmetic.",
    "NOT independent: the registered-contributors list (read with the node's registry code), the economics settings "
    "from config/genesis_config.json, the signature primitive, and (unless --quick) the node's chain validator.",
    "By default the whole chain is validated with the node's own validator, from a genesis block rebuilt on this "
    "machine and the shipped checkpoints. It proves the chain follows the rules of THIS checkout; it cannot prove "
    "that this checkout's rules, genesis file or checkpoints are the ones everybody else runs (compare the rules "
    "fingerprint and the checkpoints with people you trust).",
    "--quick does NOT prove the chain is real: it checks genesis, checkpoints, links, hashes, transfer signatures and "
    "that each hash meets the difficulty the block itself declares. A made-up history that matches the shipped "
    "checkpoints (or any block after the last checkpoint) can pass it. Use it only to look around, never to rely on a period.",
    "It cannot tell one miner from several: the mining pot follows block share, and the 2% cap does not stop a miner "
    "who uses many addresses. Watch the miner-share alert in ops/healthcheck.py for that.",
    "It cannot tell whether a registry entry is a good one. It only shows what the list is.",
    "It checks one period at a time, from the chain the node gave it. If the node hides the newest blocks, the "
    "paid-versus-recomputed check for a recent period is skipped, not failed.",
]


def _get(url, timeout=60):
    from urllib.parse import urlparse
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("URL must be http or https")
    with urlopen(Request(url, headers={"User-Agent": "netlithic-verify-payouts"}), timeout=timeout) as r:  # nosec B310
        return json.loads(r.read().decode())


def _txs(block):
    return block.get("transactions") or []


def _scratch_chain():
    """(genesis block dict, checkpoints) of THIS checkout, built in a scratch directory so nothing is written."""
    import core
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        saved = core.Blockchain.save_chain
        try:
            core.Blockchain.save_chain = lambda self: None
            bc = core.Blockchain()
            return bc.create_genesis_block().to_dict(), dict(core.get_checkpoints())
        finally:
            core.Blockchain.save_chain = saved
            os.chdir(cwd)


def check_chain(chain_list, full=True, econ=None):
    """Return a list of problems with the downloaded chain (empty = it passed).

    full=True (the DEFAULT) is the complete check: genesis block rebuilt here, shipped checkpoints, then the node's whole
    validator over every block. full=False (--quick) is the weak fast check: genesis, checkpoints, links, hashes, each
    hash against the block's own declared difficulty, transfer signatures. It writes nothing (scratch directory)."""
    import core
    from wallet import verify_signed_tx
    problems = []
    if not isinstance(chain_list, list) or not chain_list:
        return ["The node returned no blocks."]
    try:
        genesis, checkpoints = _scratch_chain()
    except Exception as e:      # a checkout that cannot even build its own genesis cannot judge anyone else's chain
        return [f"Could not rebuild the genesis block from config/genesis_config.json ({type(e).__name__})."]
    try:
        genesis_ok = core.Block.from_dict(chain_list[0]).to_dict() == genesis
    except Exception:
        genesis_ok = False
    if not genesis_ok:
        return ["The first block is not the genesis block this checkout builds from config/genesis_config.json "
                "(wrong network, wrong version of this tool, or a doctored history)."]
    for h, cp_hash in checkpoints.items():
        if h < len(chain_list) and not (isinstance(chain_list[h], dict) and chain_list[h].get("hash") == cp_hash):
            return [f"Block {h} does not match the shipped checkpoint: this is not the real chain."]
    scratch = core.Blockchain.__new__(core.Blockchain)       # only used for calculate_hash(), which reads no state
    prev = None
    for pos, b in enumerate(chain_list):
        try:
            blk = core.Block.from_dict(b)
        except Exception as e:
            return [f"Block at position {pos} is malformed ({type(e).__name__})."]
        if blk.index != pos:
            return [f"Block at position {pos} says it is block {blk.index}."]
        if prev is not None:
            if blk.previous_hash != prev.hash:
                return [f"Block {pos} does not link to block {pos - 1} (previous hash differs)."]
            if blk.hash != scratch.calculate_hash(index=blk.index, timestamp=blk.timestamp, transactions=blk.transactions,
                                                  previous_hash=blk.previous_hash, nonce=blk.nonce, difficulty=blk.difficulty):
                return [f"Block {pos}: the hash does not match the block's contents."]
            if not core.meets_pow(blk.hash, blk.difficulty, blk.index):
                return [f"Block {pos}: not enough proof of work."]
            for tx in blk.transactions:
                if core.is_signed_tx(tx) and not verify_signed_tx(tx):
                    return [f"Block {pos}: a transfer has an invalid signature."]
        prev = blk
    if full:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                saved = core.Blockchain.save_chain
                core.Blockchain.save_chain = lambda self: None
                try:
                    bc = core.Blockchain()
                    if not bc.is_valid_chain(chain_list):
                        problems.append("The node's full validator refuses this chain (balances, rewards, nonces, "
                                        "difficulty or payout blocks do not add up).")
                finally:
                    core.Blockchain.save_chain = saved
            finally:
                os.chdir(cwd)
    return problems


# ---------------------------------------------------------------------------------------------
# INDEPENDENT REIMPLEMENTATION of the payout math. Deliberately does not call contributions.py or core's payout code.
# It reads: the chain (list of block dicts), a few economics settings (`econ`), and `eligible(sender, height)` for the
# registry (shared code, see LIMITS). Everything else is spelled out below.
# ---------------------------------------------------------------------------------------------
def _is_plain_transfer(tx):
    return isinstance(tx, dict) and "payload" in tx and "type" not in tx


def independent_period(chain_list, period, econ, eligible=None):
    """Recompute one payout period. Returns a dict like the node's preview ('payouts' as {address: base units}),
    or {'status': 'none', ...}. `period` None = latest finished period."""
    se = econ.short_epoch()
    if not se:
        return {"status": "none", "error": "This chain has no payout periods."}
    fork, length = se
    height = len(chain_list)
    if period is None:
        if height - 1 < fork + length:
            return {"status": "none", "error": "No payout period has ended yet."}
        period = (height - 1 - fork) // length
    if int(period) < 1:
        return {"status": "none", "error": "Unknown period."}
    start = fork + (int(period) - 1) * length
    end = start + length
    pay_height = end + R_LAG_PERIODS * length
    out = {"period": int(period), "window_start": start, "window_end": end, "pays_at_height": pay_height}
    if height - 1 < end:
        out.update(status="period_not_finished", payouts={})
        return out
    known = [b for b in chain_list if int(b["index"]) <= end]
    # first height each address received anything (reward, transfer, payout, allocation)
    first_seen = {}
    for b in known:
        for tx in _txs(b):
            if not isinstance(tx, dict):
                continue
            r = tx["payload"].get("recipient") if "payload" in tx else tx.get("recipient")
            if isinstance(r, str) and r not in first_seen:
                first_seen[r] = int(b["index"])
    # points for blocks in [start, end)
    mined, tx_ok = {}, {}
    pair, sender_n = {}, {}
    for b in known:
        idx = int(b["index"])
        if not (start <= idx < end):
            continue
        txs = _txs(b)
        if txs and txs[0].get("type") == "reward":                  # every block pays its miner the block points
            m = txs[0]["recipient"]
            mined[m] = mined.get(m, 0) + 1
        for tx in txs:
            if not _is_plain_transfer(tx):
                continue
            p = tx["payload"]
            s_, r_, amt = p["sender"], p["recipient"], int(p.get("amount", 0))
            if s_ == r_ or amt < econ.min_point_transfer:
                continue
            if eligible is not None and not eligible(s_, idx):
                continue
            if pair.get((s_, r_), 0) >= R_PAIR_CAP:
                continue
            if pair.get((r_, s_), 0) > 0:                           # back-and-forth earns nothing
                continue
            if sender_n.get(s_, 0) >= R_SENDER_CAP:
                continue
            if idx - first_seen.get(s_, idx) < R_MIN_AGE_BLOCKS:
                continue
            pair[(s_, r_)] = pair.get((s_, r_), 0) + 1
            sender_n[s_] = sender_n.get(s_, 0) + 1
            tx_ok[s_] = tx_ok.get(s_, 0) + 1
    # the period's release budget: the community pool right after block `end`, minus its locked part, times the
    # fraction of a month the period covers
    pool_addr = econ.allocations["community"]
    bal = 0
    for b in known:
        for tx in _txs(b):
            if not isinstance(tx, dict):
                continue
            t = tx.get("type")
            if t in ("params", "pause", "registry"):
                continue
            if t in ("reward", "allocation"):
                if tx.get("recipient") == pool_addr:
                    bal += int(tx["amount"]) + int(tx.get("fees", 0))
            elif t == "multisig_spend":
                pl = tx["payload"]
                if econ.allocations[pl["bucket"]] == pool_addr:
                    bal -= int(pl["amount"])
                if pl["recipient"] == pool_addr:
                    bal += int(pl["amount"])
            elif t == "payout":
                bal -= int(tx["amount"])
                if tx["recipient"] == pool_addr:
                    bal += int(tx["amount"])
            else:
                pl = tx["payload"]
                if pl["sender"] == pool_addr:
                    bal -= int(pl["amount"]) + int(pl.get("fee", 0))
                if pl["recipient"] == pool_addr:
                    bal += int(pl["amount"])
    pool = max(0, bal - econ.still_locked("community", end + 1))
    budget = pool * (end - start) // econ.blocks_per_month
    # the two pots, pro rata by points, each address capped, then the overall per-address cap
    locked = set(econ.allocations.values())
    mine_pts = {a: n * econ.points_per_block for a, n in mined.items() if a not in locked and n > 0}
    tx_pts = {a: n * econ.points_per_tx for a, n in tx_ok.items() if a not in locked and n > 0}
    tx_budget = budget * R_TX_POT_PERCENT // 100
    mine_budget = budget - tx_budget
    mine_cap, tx_cap = mine_budget * R_POT_CAP_PERCENT // 100, tx_budget * R_POT_CAP_PERCENT // 100
    tot_m, tot_t = sum(mine_pts.values()), sum(tx_pts.values())
    pays = {}
    for a in sorted(set(mine_pts) | set(tx_pts)):
        fm = min(mine_budget * mine_pts.get(a, 0) // tot_m, mine_cap) if tot_m else 0
        ft = min(tx_budget * tx_pts.get(a, 0) // tot_t, tx_cap) if tot_t else 0
        amt = min(fm + ft, econ.payout_cap)
        if amt > 0:
            pays[a] = amt
    out.update(status="computed", budget=budget, mining_budget=mine_budget, transfer_budget=tx_budget,
               mining_cap=mine_cap, transfer_cap=tx_cap, payouts=pays,
               mining_points={a: v / 10 for a, v in mine_pts.items()},
               transfer_points={a: v / 10 for a, v in tx_pts.items()})
    return out


def paid_at(chain_list, height):
    """{recipient: amount} of the payout transactions actually in block `height`, or None if the chain has no such block."""
    for b in chain_list:
        if int(b["index"]) == height:
            return {tx["recipient"]: int(tx["amount"]) for tx in _txs(b)
                    if isinstance(tx, dict) and tx.get("type") == "payout"}
    return None


def compare_lists(mine, other):
    """Addresses whose amounts differ between two {address: amount} maps."""
    return sorted(set(mine) ^ set(other) | {a for a in set(mine) & set(other) if mine[a] != other[a]})


def independent_checks(chain_list, ind, local_prev=None, paused=False):
    """Plain-words lines and warnings from the independent recomputation."""
    lines, warns = [], []
    if ind.get("status") != "computed":
        return lines, warns
    mine = ind["payouts"]
    paid = paid_at(chain_list, ind["pays_at_height"])
    if paid is None:
        lines.append(f"The chain has not reached the paying block ({ind['pays_at_height']}) yet, so there is no paid "
                     f"list to compare with; the list below is a proposal.")
    elif paid == mine:
        lines.append(f"Independent recomputation: the {len(paid)} payout(s) actually paid at block "
                     f"{ind['pays_at_height']} match exactly.")
    elif not paid and paused:
        lines.append(f"Block {ind['pays_at_height']} paid nothing and the emergency pause was on: the coins stayed in the pool.")
    else:
        diff = compare_lists(mine, paid)
        warns.append(f"The payouts actually paid at block {ind['pays_at_height']} DIFFER from the independent "
                     f"recomputation for {len(diff)} address(es), e.g. {diff[0] if diff else '-'} "
                     f"(paid {len(paid)}, recomputed {len(mine)}). Either the node paid wrongly or this tool's maths is wrong: "
                     f"find out which before anyone relies on this period.")
    if local_prev is not None and "payouts" in local_prev:
        theirs = {p["address"]: p["amount_base_units"] for p in local_prev["payouts"]}
        if theirs == mine:
            lines.append("The node's own consensus code, run here, gives the same list as the independent recomputation.")
        else:
            warns.append("The independent recomputation and the node's consensus code disagree "
                         f"for {len(compare_lists(mine, theirs))} address(es). One of them has a bug.")
    return lines, warns


def local_preview(chain_list, period):
    """Rebuild the chain in a scratch directory and ask the real rules for this period. Writes nothing to the repo."""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        import core
        saved = core.Blockchain.save_chain
        try:
            core.Blockchain.save_chain = lambda self: None
            bc = core.Blockchain()
            if chain_list[0].get("hash") != bc.chain[0].hash:
                raise ValueError("The node's chain starts from a different first block than this checkout: "
                                 "wrong network or wrong version of this tool.")
            bc.chain = [core.Block.from_dict(b) for b in chain_list]
            return bc.payout_preview(period), core.points_v3_rules_hash()
        finally:
            core.Blockchain.save_chain = saved
            os.chdir(cwd)


def registry_notes(prev, info):
    """Plain-words lines and warnings about the registered-contributors list (TEMPORARY, CENTRALIZED).
    `info` is the node's /registry answer; None = the node has no registry (older version)."""
    if not info:
        return [], []
    lines, warns = [], []
    mode = info.get("mode")
    lines.append(f"Registered-contributors list: mode '{mode}', {info.get('active_count', 0)} active, "
                 f"{info.get('pending_count', 0)} waiting for the 1-day delay (key id {info.get('key_id') or 'none'}).")
    if mode in ("no_key", "empty"):
        lines.append("Nobody can earn transfer points yet, so the transfer pot stays in the pool. Mining points still count.")
    if mode == "on" and "payouts" in prev:
        known = {x["address"] for k in ("active", "pending", "removed") for x in info.get(k, [])}
        odd = [p["address"] for p in prev["payouts"] if p.get("transfer_points", 0) > 0 and p["address"] not in known]
        if odd:
            warns.append(f"{len(odd)} address(es) earn transfer points but are not on the list now, e.g. {odd[0]}. "
                         f"They may have been listed and removed since; check /registry and the log.")
    if info.get("adds_this_period", 0) >= info.get("max_adds_per_period", 10 ** 9):
        warns.append("The registry key has used up this period's additions. Check the log at /registry: is that expected?")
    return lines, warns


def analyse(prev, node_prev=None, node_rules=None, local_rules=None, registry_info=None):
    """Return (lines, warnings). Pure function of the two previews, so it can be tested."""
    lines, warns = [], []
    if prev.get("status") in ("none",) or "payouts" not in prev:
        return [prev.get("error", "Nothing to check yet.")], []
    pots = prev.get("pots", {})
    pays = prev["payouts"]
    total = sum(p["amount_base_units"] for p in pays)
    lines.append(f"Period {prev['period']}: blocks {prev['window_start']} to {prev['window_end'] - 1}, "
                 f"paid at block {prev['pays_at_height']} (status: {prev['status'].replace('_', ' ')}).")
    lines.append(f"Release budget for the period: {pots.get('release_budget_nlt')} NLT "
                 f"({pots.get('mining_pot_nlt')} mining pot, {pots.get('transfer_pot_nlt')} transfer pot).")
    lines.append(f"{len(pays)} addresses would be paid, {prev.get('total_nlt')} NLT in total.")
    if prev.get("status") == "skipped_or_empty":
        lines.append("The payout block for this period was mined without payouts (paused, or nothing earned): "
                     "the coins stayed in the pool.")
    if node_rules is None and local_rules is not None:
        warns.append("ERROR: the node did not report a rules fingerprint (/status). Without it nobody can tell which payout "
                     "rules that node runs, so nothing below proves anything about it.")
    elif node_rules and local_rules and node_rules != local_rules:
        warns.append("The node's rules fingerprint is different from this checkout's. Either the node runs other "
                     "payout rules or this tool is out of date. Do not trust the comparison below.")
    # 1. over the limit (each pot has its own cap; an address that earned from only one pot is held to that pot's cap)
    cap_pct = pots.get("per_address_cap_percent")
    if cap_pct is not None:
        mcap = _nlt_base(pots["per_address_cap_mining_nlt"])
        tcap = _nlt_base(pots["per_address_cap_transfer_nlt"])
        for p in pays:
            limit = (mcap if p.get("mining_points", 0) > 0 else 0) + (tcap if p.get("transfer_points", 0) > 0 else 0)
            limit = limit or (mcap + tcap)                # no points shown: fall back to the two-pot limit
            if p["amount_base_units"] > limit:
                warns.append(f"{p['address']} would get {p['amount_nlt']} NLT, more than the {cap_pct}% limit allows "
                             f"({_fmt(limit)} NLT for the pots it earned in).")
        both = mcap + tcap
        at = [p for p in pays if p["amount_base_units"] >= (mcap if p.get("transfer_points", 0) == 0 else
                                                              tcap if p.get("mining_points", 0) == 0 else both)]
        lines.append(f"Limit per address: {cap_pct}% of each pot = at most {_fmt(both)} NLT. "
                     f"{len(at)} address(es) are held at the limit." if at else
                     f"Limit per address: {cap_pct}% of each pot = at most {_fmt(both)} NLT. Nobody hit it.")
    # 2. concentration
    if total:
        top = sorted(pays, key=lambda p: -p["amount_base_units"])[:TOP_N]
        share = 100 * sum(p["amount_base_units"] for p in top) // total
        lines.append(f"The {len(top)} biggest addresses take {share}% of what is paid.")
        if share > TOP_SHARE_WARN and len(pays) > TOP_N:
            warns.append(f"The {TOP_N} biggest addresses take {share}% of the payout. Look at who they are.")
    # 3. ring look: many addresses at the sender transfer limit and no mining
    at_limit = [p for p in pays if p["transfer_points"] >= SENDER_LIMIT_POINTS and p["mining_points"] == 0]
    if len(at_limit) >= RING_AT_LIMIT_WARN:
        warns.append(f"{len(at_limit)} addresses earn only from transfers and all sit at the 30-transfer limit. "
                     f"That is what a ring of fake addresses looks like. Check them with /points_explain.")
    # 4. unspent
    budget = _nlt_base(pots.get("release_budget_nlt", "0"))
    if budget:
        unspent = 100 * (budget - total) // budget
        lines.append(f"{unspent}% of the period's release is not paid out and stays in the pool.")
        if unspent > UNSPENT_WARN:
            warns.append(f"{unspent}% of the release is unspent (the limits or a quiet network). Nothing is lost: "
                         f"it stays in the pool.")
    # 5. does the node agree?
    if node_prev is not None:
        mine = {p["address"]: p["amount_base_units"] for p in pays}
        theirs = {p["address"]: p.get("amount_base_units") for p in node_prev.get("payouts", [])}
        if mine == theirs:
            lines.append("The node's own list matches this recomputation exactly.")
        else:
            diff = sorted(set(mine) ^ set(theirs) | {a for a in set(mine) & set(theirs) if mine[a] != theirs[a]})
            warns.append(f"The node's list and this recomputation DIFFER for {len(diff)} address(es), "
                         f"e.g. {diff[0]}. Do not trust that node until this is explained.")
    r_lines, r_warns = registry_notes(prev, registry_info)
    return lines + r_lines, warns + r_warns


def _nlt_base(s):
    whole, _, frac = str(s).partition(".")
    return int(whole) * 10 ** 8 + int((frac + "0" * 8)[:8])


def _fmt(base):
    whole, frac = divmod(int(base), 10 ** 8)
    return f"{whole}.{frac:08d}".rstrip("0").rstrip(".")


def _registry_eligible(chain_list, econ):
    """(sender, height) -> bool from the chain's own registry updates up to the period's end (shared code, see LIMITS)."""
    import core
    import registry
    if not registry.REGISTRY_ENABLED:
        return None

    def build(end):
        known = [b for b in chain_list if int(b["index"]) <= end]
        return registry.resolver(registry.scan(known, econ.epoch_id, fork_height=core.POINTS_V4_HEIGHT,
                                               chain_id=econ.chain_id), core.POINTS_V4_HEIGHT)
    return build


def run_checks(chain, status, node_prev, reg_info, period, full=True):
    """Everything except the network reads. Returns (lines, warns, exit_code_floor, preview)."""
    import economics
    import core
    lines, warns = [], []
    node_rules = status.get("points_rules") or status.get("points_v3_rules")
    if not node_rules:
        return (["Could not check: the node did not report a rules fingerprint in /status. Without it there is no way "
                 "to know which payout rules it runs."], [], 2, None)
    bad = check_chain(chain, full=full)
    if bad:
        return (["Could not check: the chain this node gave us fails its own checks, so none of it can be trusted.",
                 *["  - " + x for x in bad]], [], 2, None)
    if full:
        lines.append("Chain checks passed (complete): the first block is the genesis this checkout builds, every shipped "
                     "checkpoint matches, and the node's whole validator accepts every block.")
    else:
        lines.append("QUICK MODE: the chain was only lightly checked (genesis, checkpoints, links, hashes, signatures). "
                     "It was NOT proven to carry real proof of work or follow every rule.")
    prev, local_rules = local_preview(chain, period)
    econ = economics.load_economics()
    ind = independent_period(chain, period, econ)
    if ind.get("status") == "computed":
        builder = _registry_eligible(chain, econ)
        ind = independent_period(chain, period, econ, builder(ind["window_end"]) if builder else None)
    paused = False
    if ind.get("status") == "computed":
        try:
            import pause
            paused = bool(pause.state([core.Block.from_dict(b) for b in chain if int(b["index"]) <= ind["pays_at_height"]],
                                      ind["pays_at_height"])["paused"])
        except Exception:
            paused = False
    a_lines, a_warns = analyse(prev, node_prev, node_rules, local_rules, reg_info)
    i_lines, i_warns = independent_checks(chain, ind, prev, paused)
    if not full:
        warns = ["QUICK MODE: this answer rests on a chain that was NOT fully validated. A dishonest node could have "
                 "fed a made-up history. Run again without --quick before relying on this period."] + warns
    return lines + i_lines + a_lines, warns + i_warns + a_warns, 0, prev


def main(argv=None):
    ap = argparse.ArgumentParser(description="Recompute and sanity-check one payout period (read-only).")
    ap.add_argument("--node", default="http://127.0.0.1:8000", help="node URL (GET requests only)")
    ap.add_argument("--period", type=int, default=None, help="period number (1 = first period after the fork); default: latest finished")
    ap.add_argument("--quick", action="store_true",
                    help="WEAKER and faster: skip the node's whole chain validator (never prints 'Nothing odd found')")
    ap.add_argument("--full", action="store_true", help="the complete check; this is already the default (kept so old commands still work)")
    ap.add_argument("--json", action="store_true", help="print the raw result instead of words")
    a = ap.parse_args(argv)
    node = a.node.rstrip("/")
    try:
        status = _get(node + "/status")
        chain = _get(node + "/chain", timeout=300)["chain"]
        try:
            q = "" if a.period is None else f"?period={a.period}"
            node_prev = _get(node + "/payout_preview" + q)
        except Exception:
            node_prev = None
        try:
            reg_info = _get(node + "/registry")
        except Exception:
            reg_info = None
        if a.quick and a.full:
            ap.error("--quick and --full contradict each other")
        lines, warns, floor, prev = run_checks(chain, status, node_prev, reg_info, a.period, not a.quick)
    except Exception as e:
        print(f"Could not check: {type(e).__name__}: {e}")
        return 2
    if floor == 2:
        print("\n".join(lines))
        return 2
    if node_prev is None:
        lines.append("The node has no /payout_preview (older version), so there is nothing to compare with.")
    if a.json:
        print(json.dumps({"preview": prev, "lines": lines, "warnings": warns, "limits": LIMITS}, indent=2))
    else:
        print("\n".join(lines))
        print()
        print("Things to look at:" if warns else "Nothing odd found.")
        for w in warns:
            print(" - " + w)
        print("\nWhat this check can and cannot do:")
        for x in LIMITS:
            print(" * " + x)
        print("\nThese are test coins with no value. This check only reads public chain data.")
    return 1 if warns else 0


if __name__ == "__main__":
    sys.exit(main())
