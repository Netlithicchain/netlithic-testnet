# Netlithic Economic Design (draft, partly built)

Status: proposal for discussion. Parts are implemented on the dev chain (branch `timelocks`).
Each section starts with its status. Numbers marked *proposed* are defaults for the founder to confirm,
set in `config/genesis_config.json`.

## Goals (the founder's requirements)
- Whale protection.
- No venture capital, no presale, no hidden insider allocation.
- Rewards go to people who build on and run the network.
- The community, not insiders, decides how the initial price is discovered.
- No pump-and-dump or rug-pull mechanics.

## 1. Fixed supply and genesis
**Status: built (dev chain).** See `economics.py` and `config/genesis_config.json`.
- 1 NLT = 10^8 base units (8 decimals). `MAX_SUPPLY` = 10^17 base units, fixed in code.
- The genesis block mints community 600M, treasury 150M and reserve 50M as locked allocation outputs.
- The 20% miner bucket (200M) is **not** minted at genesis. It's paid as block rewards:
  10 NLT per block, halving every 10,000,000 blocks (about 4.75 years at 15 s blocks). The series adds up
  to 199,999,998.7 NLT over 30 halving periods (about 140 years), and the code also caps rewards at
  whatever is left in the bucket. After that, block rewards are 0.
- Each allocation unlocks on a schedule by block height (1 month = 175,200 blocks), in equal monthly
  steps. Nothing beyond the unlocked amount can ever leave the address (enforced in validation):
  - *proposed* treasury (including ops/dev fund): from genesis, 48 monthly steps (4 years, 3.125M NLT/month)
  - *proposed* reserve: 1-year cliff, then 72 monthly steps (6 years, about 694,444 NLT/month)
  - *proposed* community: from genesis, 240 monthly steps (20 years, 2.5M NLT/month)
- Nodes reject any chain whose genesis doesn't match the config, or whose total minted is over MAX_SUPPLY.
- **The allocation addresses are real** Ed25519 addresses set in `config/genesis_config.json`
  (`"placeholder": false`); their keys are held offline, never on a node.

- Total supply: 1,000,000,000 NLT, fixed in code.
- Split, written into the genesis block with a public address per bucket:
  - 60% community (contribution rewards)
  - 20% validators/miners (gradual block rewards)
  - 15% treasury (includes the operations and dev fund)
  - 5% reserve
- Nodes verify the genesis allocations and supply cap on startup and reject any chain that breaks them.

## 2. Contribution rewards (community 60%)
**Status: partly built.** Points are calculated from the chain. Mining a block earns *proposed* 2.5 points
(a quarter of a transfer-heavy builder's weight). A block with no transfers, or only miner-to-self
transfers, earns 0 mining points. Each transfer sent earns 1 point. The weights are set in the genesis
config and stored in tenths, so the math is exact.
Anti-farming rules for transfer points (built; all numbers are *proposals* set in the genesis config):
- A transfer earns its 1 point only if it moves at least *proposed* 1 NLT (`min_transfer_base_units`
  = 100,000,000). Smaller transfers are still valid; they just earn nothing.
- Sending to yourself earns nothing.
- Each sender-to-recipient pair earns points for at most *proposed* 10 transfers per epoch
  (`pair_cap_per_epoch`, about a month). Paying many different people still counts; bouncing coins
  back and forth between two of your own wallets stops paying after 10.
- Known limit: someone with many wallets can still spread transfers across many pairs. The minimum
  amount and the per-address payout cap limit how much that's worth; more checks may follow.
Protocol payouts from the community pool are built:
- Every epoch (*proposed*: 175,200 blocks, about a month), the epoch block itself must contain payout
  outputs. They split the unlocked-but-unpaid community pool pro rata by the points each address
  earned in that epoch's blocks.
- Each address is capped per epoch (*proposed*: 25,000 NLT, 1% of a month's unlock). Anything over
  the cap, plus rounding, stays in the pool for later epochs.
- Every node computes the payouts the same way from the chain and rejects a block whose payouts differ.
- Payouts vest linearly (*proposed*: over 6 epochs). The unvested part can't be spent.
- The community address can never send a normal transaction. It only pays out through epochs.
- Not built yet: the code/docs, testing and community-help categories, their weights, and reviewer
  sign-off. Today only on-chain points (mining and transfers) count.

Contribution points are calculated from the chain itself, never from a side file, so they always match the chain and roll back on reorgs.

Starting weights (governance settings, changeable by vote later):

| Kind | Share of pool | How it's verified |
|---|---|---|
| Running nodes and mining | ~40% | Proven on-chain (blocks, uptime attestations) |
| Merged code and docs | ~35% | GitHub record plus sign-off from a small reviewer group (elected later) |
| Testing and bug reports | ~15% | Reviewer sign-off; security reports via security@netlithicchain.com |
| Community help | ~10% | Periodic member nominations and votes |

- Every category has a per-person cap per period so no one can farm a bucket.

## 3. Whale protection
**Status: partly built.** Per-address payout caps and vesting (section 2) are built. Governance weight
by points is not built.

- Per-wallet caps alone fail (whales split wallets), so the design leans on:
  - Vesting: earned rewards unlock gradually.
  - Per-address caps on how much can be earned from the community pool per period.
  - Governance weight based on contribution points (earned, not bought), not coin balance.

## 4. Treasury, reserve and the operations fund
**Status: partly built.** The monthly unlock schedules (section 1) are built.
- **Default: no spends.** `treasury_spend_approval.mode` is `"none"`, so nothing can leave treasury or
  reserve. The founder is building solo, and there are no independent keyholders yet.
- **Multisig is built but off.** M-of-N multisig spending (*proposed* 3-of-5) is ready for when
  independent contributors join:
  - A spend needs M distinct keyholder signatures and can never move more than the unlocked amount.
  - It carries the chain id and a per-bucket nonce, so it can't be replayed.
  - Turning it on is an explicit config change: set mode `"multisig"`, a threshold, and real keyholder
    public keys, at least two of whom are not the founder. The default config lists no keyholders at all.
- Test keyholder keys (`test_key`) and the `single_key_dev` mode are refused unless the chain id starts
  with `netlithic-dev`.
- Not built: community votes on spends (governance) and automatic spending reports.

- Treasury and reserve are time-locked in code: nothing moves before set dates, and after that only with a passed community vote.
- Operations and dev fund (part of the 15% treasury):
  - One published address.
  - Monthly unlocks over about 4 years, enforced by code.
  - Public spending reports.
  - Budget moves to community governance once it exists.

## 5. Founder share
**Status: not built.** There is no founder allocation in genesis.

- The founder earns contribution points like any builder, under the same vesting and caps.
- Any separate founder share must be small, published at genesis, and locked longer than anyone else's. Nothing hidden.

## 6. Price discovery
**Status: nothing to build.** The code sets no price.

- The code does not set a price.
- Fair launch: coins are only earned (mining and contribution); no presale.
- If and when trading starts, the method is chosen by community vote, well after testnet.

## 7. Funding the project
Revenue first, tokens last.
- Revenue sources: paid node hosting, developer tools and APIs, support plans, and outside grants (ecosystem and open-source grants).
- The project is not funded by selling tokens. Treasury spending stays off until independent keyholders exist.

## 8. How builders earn
- Contribution rewards from the community pool (section 2).
- Their own apps and services on the network: hosting nodes for others, tools, APIs, AI agents, and paid support, keeping what they earn.
- Grants funded from the treasury by community vote.

## 9. Transaction fees (PROPOSAL, built on branch `testnet-prep`)
- Every transfer pays a fee, minimum 0.0001 NLT (proposal; set in the genesis config).
- The sender pays amount + fee. The miner of the block keeps all its fees on top of the block reward.
- Fees are transfers, not new coins, so the 1B cap holds. They keep paying miners after the miner
  bucket's block rewards run out.
- Miners pick the highest-fee transfers first (blocks hold up to 1000 transfers).
- Contribution points are unchanged: the fee doesn't count toward the 1 NLT minimum for points.
- Decided (founder, 2026-10-21): launch with 100% of fees to miners. A later governance vote will decide whether a small share goes to the community pool.

## Open questions
- Exact vesting length and per-period caps.
- How node uptime is attested without being faked.
- Who the first reviewers are and how they're elected.
- Transition from PoW block rewards to the 20% validator schedule.
