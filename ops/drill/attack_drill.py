"""Private local 51% / deep-reorg drill. Box only: no network, no real wallets.

Runs several independent Blockchain node instances in a throwaway directory,
lets an honest miner build a chain that all nodes accept, then has a majority
attacker mine a secret heavier fork and broadcast it. Runs every scenario twice:
"before" (security fork inactive) and "after" (fork active, reorg limit,
checkpoints, hardened stall rule).

Usage:  python ops/drill/attack_drill.py [--json out.json]
"""
import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

HONEST, ATTACKER = "a" * 32, "e" * 32


def setup(mode, reorg_limit=20, checkpoints=None):
    import core
    core.LWMA_FORK_HEIGHT = 3
    core.LWMA_MIN_WORK = 16
    core.SECURITY_FORK_HEIGHT = 3 if mode == "after" else 10**9
    core.MAX_REORG_DEPTH = reorg_limit
    path = os.path.join(os.getcwd(), "config", "checkpoints.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if checkpoints:
        with open(path, "w") as f:
            json.dump(checkpoints, f)
    elif os.path.exists(path):
        os.remove(path)
    core.Blockchain.save_chain = lambda self: None   # keep nodes in memory only
    return core


def mine(core, bc, chain, n, start_ts, step, miner):
    from conftest import build_block
    chain = list(chain)
    ts = start_ts
    for _ in range(n):
        ts += step
        txs = [core.make_reward_tx(miner, bc.expected_reward(chain))]
        chain.append(build_block(bc, txs, chain=chain, timestamp=ts))
    return chain


def reorg_scenario(mode, depth, limit=20, n_nodes=4, checkpoint_at=None):
    core = setup(mode, limit)
    nodes = [core.Blockchain() for _ in range(n_nodes)]
    genesis = nodes[0].chain
    t0 = time.time() - 40 * 3600
    honest = mine(core, nodes[0], genesis, depth + 30, t0, 60.0, HONEST)
    for n in nodes:                       # honest blocks gossip one at a time
        for b in honest[1:]:
            assert n.add_block(b.to_dict()), n.last_reject_reason
    fork_point = len(honest) - depth
    if checkpoint_at is not None:      # checkpoint a block inside the range the attacker rewrites
        checkpoint_at = fork_point + checkpoint_at
        setup(mode, limit, {str(checkpoint_at): honest[checkpoint_at].hash})
    # attacker: ~2x hashrate -> blocks every 30s, mined secretly from fork_point
    secret = mine(core, nodes[0], honest[:fork_point], depth + 5,
                  honest[fork_point - 1].timestamp, 30.0, ATTACKER)
    assert nodes[0].chain_work(secret) > nodes[0].chain_work(honest)
    accepted = 0
    reasons = set()
    for n in nodes:
        if n.replace_chain([b.to_dict() for b in secret]):
            accepted += 1
        else:
            reasons.add(n.last_reject_reason or "rejected by chain validation (checkpoint)")
    return {"scenario": f"reorg depth {depth}" + (f", checkpoint at {checkpoint_at}" if checkpoint_at else ""),
            "mode": mode, "reorg_limit": limit if mode == "after" else None,
            "nodes": n_nodes, "nodes_reorged": accepted,
            "honest_blocks_erased_per_node": depth if accepted else 0,
            "reasons": sorted(reasons)}


def stall_scenario(mode):
    """Backdate the previous block so a 'stall' looks 10 minutes long and claim
    1/8 work, without the network actually having stalled."""
    core = setup(mode)
    from conftest import build_block
    bc = core.Blockchain()
    now = time.time()
    hist = mine(core, bc, bc.chain, 12, now - 25 * 60 - 120, 120.0, HONEST)
    mtp = core.median_time_past(hist)
    back = build_block(bc, [core.make_reward_tx(ATTACKER, bc.expected_reward(hist))],
                       chain=hist, timestamp=mtp + 1)
    ok_back = bc.block_reject_reason(back, hist[-1], hist[:-1]) is None
    hist2 = hist + [back]
    ts = min(now, back.timestamp + core.STALL_SECONDS)
    allowed = bc.allowed_difficulties(hist2, ts)
    return {"scenario": "fake stall by backdating", "mode": mode,
            "backdated_block_valid": ok_back,
            "cheap_1_8_work_allowed": len(allowed) == 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    results = []
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        for mode in ("before", "after"):
            for depth in (6, 15, 40):
                results.append(reorg_scenario(mode, depth))
            results.append(reorg_scenario(mode, 10, checkpoint_at=2))
            results.append(stall_scenario(mode))
    for r in results:
        print(json.dumps(r))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
