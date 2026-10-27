# Points v4 registry: only listed contributors earn transfer points

**TEMPORARY and CENTRALIZED.** One key decides who is on the list. That is a deliberate stop-gap for the testnet, not a
design for a real network. It exists because points v4 alone does not stop a ring of fake addresses (see below).

**Status: built on the local branches `points-v4-registry` (on top of `points-v4-protection`) and `points-v4r-fixes` (security-review fixes H2, below). Nothing is pushed, tagged or
deployed, and the signing key slot is EMPTY until the founder provides a public key.** Test coins have no value. Nothing here is
about price, return or investment.

## In plain words

* Points v4 pays 20% of each day's release for **transfers** and 80% for **mining**. A ring of 400 fake addresses that
  pass coins around still collects most of the transfer pot (about 4,089 of one day's 20,548 NLT in the simulator).
* This option adds one rule: **a transfer only earns points if the person sending it is on the registered list.**
  Everybody else can still send, receive and mine. They just do not get transfer points.
* **Mining is untouched.** Mining costs real work, so anyone can mine and earn mining points, listed or not.
* How you get on the list is a human decision by the founder (see "How to get listed" below). It is meant to be quick and
  friendly for people who are not into crypto: you send your address, a person looks at it, and a day later you count.
* An address added today starts counting **one day later** (1,440 blocks). Everybody can see the waiting list at
  `/registry` and in the explorer, so a bad addition is visible before it can earn anything.
* An address can be **removed**. A removal works the same way as an addition, one day later, and **only looks forward**: it
  can never take away points somebody already earned. (The first version erased the unpaid day, which meant a stolen key could
  wipe out a whole day of honest earnings in one message. That was security-review finding H2 and is fixed.)
* If the key is not set, or the list is empty, **nobody earns transfer points**. The unspent coins stay in the pool.
  The rule never opens up by accident.
* Before launch, a single constant switches the whole thing off (`registry.REGISTRY_ENABLED = False`): the transfer pot is then
  open to everybody, exactly as in points v4. **After launch it is not a safe switch.** Once any registry transaction has been mined,
  flipping `REGISTRY_ENABLED` to False makes every node that has the change treat that block as invalid and every node that does not
  treat it as valid: that is a **hard fork** (same procedure as any rules change; everybody upgrades before an announced height).
  After launch the safe, planned way to open the transfer pot again is `REGISTRY_SUNSET_HEIGHT` (a height written into the release,
  announced well ahead, with no registry transaction needing to become invalid).

## What the registry does not fix (read this)

| Still unprotected | Why |
|---|---|
| **Fakes that really mine.** | Mining points are open. An attacker with real hash power who spreads blocks over many addresses earns from the mining pot in proportion to the blocks mined (simulator: 10% of the blocks = 10% of the mining pot, 1,644 NLT). The 2% limit per address does not stop spreading. |
| **Bad registrations.** | The list is only as good as the review. In the simulator, 50 fake addresses on the list (one period's maximum) take 3,952 NLT, almost what the open pot paid the ring (4,089). |
| **Trust in one key.** | Whoever holds the registry key decides who earns. A stolen key is limited (50 additions and 50 removals in any 1,440 blocks, 1-day delay both ways, visible at `/registry`), not harmless. A key can be changed on the chain by a signed **rotation** (below), but a rotation signed by the old key cannot beat a thief who also holds that key: for that the pause key stops payouts and the break-glass plan below applies. |
| **Centralization.** | It is a permissioned list. That is the opposite of an open network, which is why it is labelled temporary and has an optional sunset height. |
| **Honest people who are not listed yet** | They earn mining points but no transfer points until they are on the list and a day has passed. |
| **Waiting at the start** | Registry updates are only valid from block 5000 and need 1,440 blocks of delay, so no transfer points can be earned in the first payout period (blocks 5000 to 6439, paid at block 7880). The first transfer points are paid at block 9320. |

## Three designs compared

| | 1. List embedded in each release | 2. Signed list on the chain (**chosen**) | 3. Signed list fetched by nodes |
|---|---|---|---|
| How nodes learn the list | It is in the code | Signed `registry` transactions inside blocks | Each node downloads a signed file from a URL |
| Do all nodes agree? | Yes, if everybody runs the same release | Yes: the list is computed from the chain, like the pause switch | **No**: two nodes can fetch different files at different times, and then they pay different people. Consensus can split |
| Reorg safe | Yes | Yes (a reorg re-derives the list from the blocks) | No |
| Changing the list | New release for every change, everybody must upgrade, and the one-day clock starts only when nodes upgrade | One signed message, no upgrade | One new file |
| Delay and limits enforced by | Social process | The chain itself (1-day delay, 50 additions and 50 removals per rolling day, 500 total, key rotation) | Nothing, unless every node checks the same rules |
| Public audit trail | Git history | Every change is in the chain and at `/registry` | A file on a server |
| Needs an off-chain server | No | No | Yes: a new thing to attack or lose |
| Key custody | Release signer | One registry key, kept offline | Same, plus the file host |

Why design 2: it is the only one where every node always agrees, where the one-day delay and the addition limit are
enforced by the chain and not by trust, and where changing the list does not need a release. It copies the way the
pause switch already works (a public key in the code, signed messages on the chain, a nonce so nothing can be replayed),
so the review and the tooling already exist. Its cost is that it needs a new fork-gated transaction type.

## Exact rules (what every node enforces)

* **Who may sign:** only the key in `registry.REGISTRY_PUBKEY` (an empty slot until the founder supplies a **public** key).
  Use a different key from the pause key. The signed message is domain-separated from the pause message and includes the
  chain id, so a pause signature or a signature from another chain can never be used for the registry.
* **The transaction:** `{"type":"registry","adds":[...],"removes":[...],"reason":"...","nonce":n,"valid_until":h,"pubkey":...,"sig":...}`
  (and optionally `"new_key": <public key>` for a rotation, which stands alone: no adds or removes in the same update).
  Nonces go 1, 2, 3 and so on. At most 50 addresses per transaction, at most 500 addresses listed at once. The reason (1 to
  280 characters) is public.
  Adding an address that is already listed, or removing one that is not, is invalid.
* **`valid_until` (required):** the highest block height that may carry the update. A signed update that sits in a
  drawer cannot be mined much later. The signed message also contains the chain id, so it cannot be replayed on another chain.
  The tools set it to 2,880 blocks (two days) ahead by default. **Consensus caps it** (review R3): a registry update is invalid if
  its `valid_until` is more than **10,080 blocks (seven days)** above the height of the block that carries it, so a signature
  made with "valid for a million blocks" can never sit in a drawer for months. (`REGISTRY_MAX_VALID_BLOCKS`; part of the rules
  fingerprint, so the `points_rules` value changed with this fix and every node must upgrade together.)
* **Limits (rolling, not calendar):** in **any** 1,440 consecutive blocks at most 50 additions and at most 50 removals are
  valid. Counting over a rolling window means there is no double allowance around the edge of a payout period (the first
  version counted per period, so a key could add 50 at the end of one period and 50 more at the start of the next).
* **Where it sits in a block:** after the payouts and any pause transactions, before multisig and normal transfers.
  Registry transactions are valid only from block 5000 (the points v4 fork). Before that they make the block invalid.
* **Which side earns: the SENDER.** Transfer points already go to the sender only, so the sender must be listed at the
  block that carries the transfer. The receiver is not checked: an honest listed person can pay a shop, a friend or a
  new user who has never heard of the registry, and nobody has to explain anything to the receiver. Checking the receiver
  would also let a fake ring earn by paying one listed address, and would punish normal people for who they pay.
  An unlisted sender's transfer earns nothing **and uses up none of the caps** (it is as if it was not there).
* **Addition delay:** an address added in block h counts for transfers mined at heights h + 1,440 and later.
* **Removal (prospective only):** a removal in block h stops the address earning for transfers mined at heights
  h + 1,440 and later. Transfers it made while it was listed keep their points, even if the removal is mined before they are
  paid. A removal of an address that is still waiting for its delay cancels the addition. Re-adding restarts the clock.
  The list that pays a period is therefore fixed by that period's own blocks: the registry is read as of the last block of the
  period, which gives the same answer as reading it at the paying block (a later update cannot change an earlier transfer).
  Trade-off, on purpose: a removal also takes a day to bite, so a thief who has just listed fakes can earn for one more day
  after the owner notices. The pause key (stop payouts) is the fast brake.
* **Key rotation (no fork):** `REGISTRY_PUBKEY` is only the **first** key. The current key can sign an update with
  `new_key`; the new key takes over 1,440 blocks after the block that carries it, and until then the old key keeps working.
  **The new key must be a real Ed25519 public key** (review R1): 64 hex characters that decode to a proper point on the curve,
  not a small-order point and not a made-up number. Consensus checks this with the same function the tool uses
  (`wallet.is_valid_pubkey_hex`), so a typo can no longer hand the registry to a key nobody can sign with.
  **The new key must also co-sign the handover** (`new_sig`, a second signature, domain-separated as `registry_accept`
  so it can never be mistaken for a normal update signature): only someone who really holds the new private key can accept it.
  A pending rotation can be replaced by another one signed by the (still current) key. The new key must differ from the current
  key and from the pause key. After the switch the old key is invalid. Which key is current is derived
  from the chain, so every node agrees. Tool: `python ops/registry_tool.py rotate ...` (signing stays offline).
  Offline flow (no private key ever touches a server or the tool):
  1. `python ops/registry_tool.py rotate NEWKEY --nonce N [--node URL] > unsigned.json` writes the unsigned update (it refuses a new key that is not
     a valid Ed25519 point).
  2. On the offline machine of the **current** key: `python ops/registry_sign_offline.py unsigned.json` prints the signature.
  3. On the offline machine of the **new** key: `python ops/registry_sign_offline.py unsigned.json --as-new-key` prints the
     co-signature.
  4. `python ops/registry_tool.py assemble unsigned.json --signature SIG --new-key-signature NEWSIG > tx.json` (the assembler
     refuses a rotation without the co-signature, and the co-signature on anything that is not a rotation).
  5. `python ops/registry_tool.py check-tx tx.json`, then submit the transaction.
* **Mining points:** not affected by the registry at all. Note that a block still counts as "mined for others" when it
  contains somebody else's transfer, listed or not.
* **Caps and lag:** unchanged from v4 (2% per address per pot per day, 1-day payout lag, first payout at block 7880). **The transfer pot and the mining pot both pay by the same per-address rules; the registry gates only the transfer pot.**
* **Fail safe:** registry on with an empty key slot, or with nobody active, means no transfer points. The transfer pot
  stays in the pool. No registry transaction is valid while the slot is empty.
* **Pause:** the day keeps counting in blocks while payouts are paused. A registry update during a pause is a valid block
  and takes effect as normal; the paused period itself pays nothing (as in v4).
* **Switches (consensus constants):** `REGISTRY_ENABLED` (False gives exactly points v4: the rules fingerprint is then
  byte-for-byte the v4 fingerprint), `REGISTRY_DELAY_BLOCKS` (1,440), `REGISTRY_REMOVE_DELAY_BLOCKS` (1,440), `REGISTRY_LIMIT_WINDOW` (1,440),
  `MAX_ADDS_PER_WINDOW` (50), `MAX_REMOVES_PER_WINDOW` (50), `REGISTRY_ROTATION_DELAY_BLOCKS` (1,440), `MAX_ENTRIES_PER_TX` (50),
  `MAX_ACTIVE_ENTRIES` (500), `REGISTRY_SUNSET_HEIGHT` (None = never; otherwise the transfer pot opens again from that
  height, which must be announced well in advance).
* **Rules hash and handshake:** the registry settings (on/off, key, delay, limits, sunset) are part of the `points_rules`
  fingerprint shown in `/status` and checked in the `/hello` handshake, so a node with a different list rule cannot
  join. The *list itself* needs no fingerprint: it comes from the blocks.

## How to get listed (plain words, for people who are not into crypto)

Nothing here is built yet: no form, no channel and no message has been created or posted. the founder decides how requests come
in. The simplest version:

1. **Ask.** The person sends their wallet address (the 32-character code they see in the wallet) and one line on what
   they will do with the testnet ("I will send test coins to my friends and try the shop"). Options: a short form, a
   message in the Discord, or an email. They never send a password, a recovery phrase or a key, and nobody should ask.
2. **Review (a few minutes).** the founder (or someone the founder names) checks:
   * it is a real person or a real group they know or can reach, not a bot;
   * one address per person at the start (a second one needs a reason);
   * the address is a normal wallet address and has not been listed before;
   * nothing in the history of the address looks like a farm (many addresses all paying each other).
3. **Add.** The approved addresses are put into a list file (`ops/registry/registry_list.json`). At most 50 can be added in
   any one day, which is also the pace a human can review.
4. **Sign offline.** the founder signs the update on his own offline machine. The build computer never sees the key.
5. **Wait one day.** The address shows as "pending" at `/registry` and in the explorer. After 1,440 blocks it counts.
6. **Tell them.** The person is told their address, the date it starts counting, and that mining points always count.

**Appeal and removal.** Anyone can ask why they are not listed or ask to be removed. A removal is a signed update like an
addition. Reasons for removal: the person asks; the address is part of a farm; the key holder made a mistake. A removal
applies from one day later and never to points already earned. There is no penalty beyond losing future transfer points.

## Tools

| What | Command |
|---|---|
| Release gate (fails while the key slot is empty) | `python ops/registry_tool.py --check` |
| Put the PUBLIC key in the slot (checks it is a valid public key; refuses the pause key and refuses to replace a key already set; it cannot tell a private key from a public one, so copy the line labelled PUBLIC) | `python ops/registry_tool.py set-key <public key>` |
| Check a list file | `python ops/registry_tool.py verify ops/registry/registry_list.json` |
| Work out what must change (save `/registry` from a node first) | `python ops/registry_tool.py build LIST.json --nonce N --registered registry.json > unsigned.json` |
| Sign (on the key holder's own offline machine; shows what it signs and asks first) | `python ops/registry_sign_offline.py unsigned.json` |
| Attach the signature (checks it against the slot key) | `python ops/registry_tool.py assemble unsigned.json --signature HEX > tx.json` |
| Submit | the founder posts `tx.json` to a node's `/submit_tx`. **No tool here does this.** |
| What the chain says | node `/registry`, `/registry?address=...`, explorer page "Registry" |
| Payout sanity check | `ops/verify_payouts.py` now also reports the registry mode and warns when transfer earners are not on the list |

No private key is generated, read or stored on the build computer by any of these tools. Tests use throwaway keys made
inside the test only.

## Numbers (simulator, one day of release = 20,548 NLT: 4,110 transfer pot, 16,438 mining pot)

See `ops/sim/points_sim.py --registry` (output saved with the report). Highlights:

| World | v4 open pot: fakes get | v4 + registry: fakes get | Listed honest users |
|---|---|---|---|
| 400-fake ring, hub funded | 4,089 NLT (80%) | **0** | 20 listed users: 1,644 NLT instead of 20 |
| 400 mining-born fakes | 4,089 NLT (80%) | **0** | same |
| Ring plus fakes that mine 10% of blocks | 5,733 NLT | 1,644 NLT (the mining pot, open by design) | same |
| 10 of the ring got listed by mistake | 4,089 NLT | 822 NLT | 685 NLT |
| 50 of the ring got listed by mistake | 4,089 NLT | 3,952 NLT | 158 NLT |
| Nobody listed (empty list) | n/a | 0 transfers, mining still paid | 0 |

Because of the 2% limit per address, 20 honest listed users take 40% of the transfer pot and the rest stays in the pool.

## If the registry key is lost or stolen (break-glass plan)

Be honest about what the chain can and cannot do:

1. **Key lost, not stolen.** If the key is truly gone, nobody can sign a rotation. The list freezes as it is (already-listed
   addresses keep earning, nothing new can be added or removed). Fix: a coordinated **hard fork**: a new release with a new first
   key in `REGISTRY_PUBKEY` (or a `REGISTRY_SUNSET_HEIGHT` to open the pot; note that setting `REGISTRY_ENABLED = False` is itself a
  hard fork once any registry transaction has been mined), announced well ahead,
   every node upgrades before the activation height. This is the same procedure as the v4 upgrade and needs everybody.
2. **Key stolen, owner still has it.** Race: the owner signs a rotation to a new key. The thief can sign a competing rotation
   with the same old key, and the later block replaces the pending one, so the owner cannot *win* a race against a thief
   who keeps re-signing. A rotation therefore helps when the key is **lost or suspected**, not against an active thief.
3. **Key stolen, thief active.** Steps, in order: (a) pause payouts with the pause key (the paused period is skipped and its
   coins stay in the pool); (b) read `/registry` to see what was added (pending additions are visible for a day, at most
   50 per rolling day); (c) hard-fork with a new key or with the registry off, as in item 1; (d) remember that a removal also
   takes a day, so the fast brake is the pause, not a removal.
4. **What nothing here can do:** undo coins already paid. Payouts are final once the paying block is buried.

Nothing in the code can do these steps by itself. They are a human plan; test them in a drill before the key is set.

## Upgrade notes

* The registry changes the rules fingerprint: **every node must upgrade together** (same as for v4). The shipped default is
  `REGISTRY_ENABLED = True` with an empty key slot, which is the fail-safe state; run `registry_tool.py --check` before
  any release and fill the slot first, or the transfer pot pays nobody.
* To ship v4 exactly as it is **before any registry transaction exists**, set `REGISTRY_ENABLED = False`. **After launch that is a
  hard fork** (blocks carrying registry transactions become invalid); use `REGISTRY_SUNSET_HEIGHT` as the safe switch.
* Changing the **first-key constant** later invalidates anything signed by the old key and is a fork. Treat it as set once. Later key changes are done with a rotation (no fork).
* The registry cannot be used before block 5000, and nothing counts for a day after it is added.
