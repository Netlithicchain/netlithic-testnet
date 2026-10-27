> **Superseded by points v4** (20/80 pots, 2% per-address cap, one-day payout lag; first payout at 7880, family rule off). v3 never shipped. This page stays as the history of the v3 design; the numbers below (30/70, first payout at 6440, `POINTS_V3_HEIGHT`) are the old ones.

# Points v3 (consensus rules for payouts at height >= `core.POINTS_V3_HEIGHT` = 5000)

Evaluated at the PAYOUT height, so every payout at or after 5000 (including the first, at 6440) uses v3 for its
whole 1440-block window. `POINTS_V3_HEIGHT` equals `SECURITY_FORK_HEIGHT` on purpose: the live leaderboard then shows the
rules that pay block 6440, and no payout can fall between two different heights (review L1).

## What is switched on in the shipped build

| Piece | State | Why |
|---|---|---|
| Slow release (about 0.82% of the available pool per 1440 blocks) | **ON** | The real protection: bounds any farm |
| Two pots (30% transfers `TX_POT_PERCENT`, 70% mining) | **ON** | Fakes can never touch the mining pot |
| 25,000 per-address cap | **ON**, unchanged | |
| Funding-family rule (`FAMILY_RULE_ENABLED`) | **OFF** (code, issuer list and tests stay) | See "Recommendation": it stops only the laziest farm |
| Pair cap between families (`FAMILY_PAIR_CAP`) | OFF (None) | Costs honest users more than it costs attackers |
| Per-point ceiling (`MAX_BASE_PER_POINT`) | OFF (None); decision for the founder | Stops a quiet network paying its whole pot to one person |

`core.points_v3_rules_hash()` fingerprints every constant above plus the issuer list. It is shown in `/status`
(`points_v3_rules`) and `/hello` refuses a peer whose value differs (review M4), so two builds that would pay differently
cannot become peers. Peers that do not report it are older builds; the version gate handles them. This is a handshake
check only, not consensus: it does not stop a block pushed by a node that never says hello.

## The rules in order

1. **Slow release (`Blockchain._v3_payouts`).** Each period releases `available_pool * period / blocks_per_month`
   (about 0.82% of the pool per 1440 blocks), not the whole pool pro rata. This is **not** "lasts 4 months": each
   month-step (2.5M NLT) is 63% released after its own 121.7 days (1 - e^-1); the rest carries into the next month. Average
   release is 20.5k NLT/day; the day after a month-step it peaks around 32.5k/day.
2. **Two pots (`contributions.v3_amounts`).** 30% of the release is shared by transfer points, 70% by mining points; each
   address is still capped at `payout_cap`. A pot nobody earns from, rounding and cap excess stay in the pool.
3. **Optional ceiling per point (`max_per_point`, review M2).** A pot never pays more than this per whole point. On a
   quiet network one transfer would otherwise collect the whole transfer pot (about 6.1k NLT). Default off.
4. **Funding families (only if `FAMILY_RULE_ENABLED`; `contributions.family_roots`).**
   * Chain-issued coins (reward, payout, genesis allocation, treasury/reserve `multisig_spend`) make the recipient its own root.
   * A transfer counts as funding only when the recipient's *running total* received reaches 1 NLT (so splitting funding into
     dust pieces does not hide a farm). The transfer that crosses the line decides: a sender on the issuer list gives the
     recipient a family of its own; a sender that already has a family gives *that* family; a sender with **no family** decides
     nothing (review H1: the first draft made such a sender its own root, which let dust-staged intermediaries create
     unlimited families).
   * An address with no family ("unrooted") earns no transfer points until a funder with a family pays it 1 NLT or more.
   * A transfer inside one family earns 0 and does not use up the pair/sender caps.
   * **Fail-safe:** if the issuer list is empty, or no listed issuer ever sent a transfer of 1 NLT or more on this chain,
     `active_family_roots` returns None and the build falls back to design A alone (no families). Wrong list = weaker, never "honest users earn 0".

## Issuer list: where each address comes from (public sources only)

| Address | Role | Public source |
|---|---|---|
| `e924523b2614a39b9bb4ff8bfc05e747` | founder server miner and faucet | explorer founder page, repo files, and 6 faucet drips of 10 NLT on the public chain |
| `f953a95407a5ae8bba986101d1f9b272` | genesis community allocation (locked) | `config/genesis_config.json`, `/status` buckets |
| `e26742c7bcdb96fa0ca9446073b04a35` | genesis treasury allocation (locked) | same |
| `65ca7858ba65ee7a27d8b2879197d973` | genesis reserve allocation (locked) | same |

No separate faucet wallet address appeared in any public source: on chain the drips come from `e924523b…` itself.
**Open item for the founder:** `TESTNET_TOOLS_DEPLOY.md` says the faucet may use its own wallet. Run `curl http://localhost:8090/api/info` on the server
(public data, no wallet file) and compare with this list before the family rule is ever switched on. If it differs, add it.

### H2: startup refusal and release gate
* `contributions.validate_family_issuers()` runs in `Blockchain.__init__`: an empty, malformed (not 32 lowercase hex) or
  duplicated list stops the node with `FamilyIssuerError`. A bad list in a build is a build error, not something to run on.
* Release gate test: `tests/test_points_v3_hardening.py::test_release_gate_issuer_list_is_non_empty_and_valid`.
* Release check against a chain: `python ops/check_family_issuers.py http://host:8000` (read only). It fails when no listed issuer funded
  anyone and warns about unlisted addresses that sent 1 NLT or more to 3+ people.
* How the two safeguards fit: *malformed or empty* = refuse to start (a developer error, found before release). *Well-formed but wrong*
  (for example a faucet wallet that was rotated) = the consensus fail-safe to design A. Because the list is consensus data, a wrong
  entry cannot be fixed silently later: adding an issuer changes who earns, so it needs a release that every node installs before
  the first payout that uses it (at least 14 days of notice, same as any fork), and `points_v3_rules` in `/status` shows who is on which list.

## Dust and poisoning (asks 3)
* Under 1 NLT: cannot root, cannot poison. Splitting funding into pieces (3 x 0.4 NLT): the running total roots the address to the funder.
* A transfer of 1 NLT or more to a brand-new address *does* decide its family (that is the rule). An attacker can therefore
  "poison" honest newcomers by paying each 1 NLT before their faucet drip, so the victims all join the attacker's family and
  their transfers to each other earn 0. Cost: 1 NLT + fee per victim; effect: only the victims' payments *among themselves*
  (payments to shops are unaffected). Tested. A higher funding minimum would let farms stay just below it, so the minimum stays at 1 NLT.

## Honest-user effects of the family rule (review M3; only if switched on)
* A transfer between two addresses of one family earns 0: a gift from whoever funded you, a parent paying a child, two
  wallets of one person.
* One early gift decides the family for ever: whoever sent you your first 1 NLT or more is your family root.
* Exchange hot wallet / pool operator / faucet: customers it funded are one family, so *customer-to-customer* payments earn 0 (customer to shop still
  earns). Simulated: 50 exchange customers lose 50 of 200 points (200 -> 150).
* Treasury / reserve grants (`multisig_spend`) each start their own family.

## Recommendation on the family rule (review M1), with numbers

Simulator (`python ops/sim/points_sim.py`, part 2; 120 fakes; pot = 20,548 NLT/day, transfer pot 6,164):

| Attack | A only | family rule, first draft | family rule, hardened | hardened + pair cap 10 |
|---|---|---|---|---|
| hub ring, 1 root | 30% (6,063) | 0 | 0 | 0 |
| H1 dust-staged, 1 hub | 30% | **30%** (broken) | 0 | 0 |
| M1 two unrelated roots | 30% | 30% | **30%** | 7% |
| 12 roots | 30% | 30% | 30% | 26% |
| 60 roots | 30% | 30% | 30% | 29% |
| honest: 50 exchange customers' points | 200 | 150 | 150 | **30** |

Plain words: the family rule only beats a farm that is funded from a single wallet. An attacker with two funders (two mined blocks or two
faucet drips, a few minutes' work) is back to 30% of the transfer pot, which is the same ceiling design A already gives. The pair cap helps only
against 2 roots and hurts honest exchange customers by 80%. Every family-rule variant adds a consensus list that can only be changed by a
fork, and a new way for honest users to earn 0. **Recommendation: DROP the family rule from the shipped build (it is off), keep the
code, tests, issuer list and `ops/check_family_issuers.py` so it can be enabled by a later announced fork if a lazy single-hub farm
shows up in practice; do not use the pair cap.** What really bounds farming is the slow release plus the two pots: any farm is capped at the transfer pot
(about 6.2k NLT/day, 30% of a day's release), and mining rewards stay untouchable. "Keep it hardened" is the alternative if the founder wants
protection against the single-wallet farm, accepting the issuer list and the honest-user effects above.

The earlier claim "one mined block per fake" was wrong: farms need one funder per *family*, not one per fake address.

## Explanation API (ask 4)
`GET /points_explain?address=` says in plain words why transfers earned 0 (self, under the minimum, unrooted, same family, family pair cap, pair cap, reciprocal,
sender cap, new address) and how many mined blocks earned 0 because they were empty. The explorer address page shows the same under "Why did this earn 0?".

Unchanged: point values, 1 NLT minimum, pair cap 10, sender cap 30, reciprocal rule, 1440-block new-address wait, empty-block rule
(B5, see POINTS_FIX.md), vesting, pause switch. Simulator: `python ops/sim/points_sim.py`.
