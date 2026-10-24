# Confirmations guide (exchanges, bridges, anyone crediting deposits)

Netlithic is a small proof-of-work test network. A small network can be
out-mined by one well-funded party. Treat confirmations as the main defense.

## What the node enforces (from height 5000, `SECURITY_FORK_HEIGHT`)

| Rule | Value | Effect |
|---|---|---|
| Reorg depth limit | `MAX_REORG_DEPTH = 720` blocks (~12 h at 60 s) | A node at or past 5000 refuses any competing chain that would drop more than 720 of its blocks, even if that chain has more work. |
| Checkpoints | `config/checkpoints.json` (`{"height": "hash"}`) | Any chain whose block at a checkpoint height has a different hash is invalid. The file is optional and empty by default. |
| Stall rule | 10 min gap, measured from the newest of the last 11 blocks, never future-dated | Cheap 1/8-work blocks only after a real 10-minute silence. |

The reorg limit is a "finality by local policy" rule. It protects nodes that
were online and synced. A node that was offline and comes back to two chains
just follows the heavier one. If the network ever splits more than 720 blocks (~12 h)
deep, it will not heal by itself and has to be fixed by hand (see
`INCIDENT_PLAN.md`).

## Recommended confirmations

Target block time is 60 s.

| Deposit size (test coins) | Wait | Approx. time |
|---|---|---|
| Small / faucet amounts | 60 blocks | ~1 h |
| Normal | 120 blocks | ~2 h |
| Large, or anything you can't take back | 721 blocks (past the reorg limit) | ~12 h |

**Default: wait 60-120 confirmations; more for large deposits.** Waiting 721 blocks
means an attacker would have to reorg past the limit, which every synced, upgraded
node refuses. Block times can be slower than 60 s when few people mine; count
blocks, not minutes.

## Also do this

* Run your own node; never credit deposits based on someone else's explorer.
* Check `GET /status` height on 2+ independent nodes before crediting.
* Watch for alerts: when a node rejects a deep reorg it logs
  `Rejected chain: reorg of N blocks exceeds limit 720`. That means someone
  is attacking or the network split, so stop crediting and contact the operators.
* Check `GET /pause_status`. If payouts are paused, read the reason.
* Halt deposits if hashrate jumps a lot or block times collapse.

## Upgrade requirement

The rules turn on at height 5000. Every node, miner and exchange must run
this version **before** the chain reaches 5000, or it will fork away.
