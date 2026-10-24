# Incident plan

For the Netlithic test network. These are test coins with no value. The goal
is to keep the chain honest, keep users informed, and recover cleanly.

## Severity

| Level | Examples | Response time |
|---|---|---|
| SEV1 | Deep reorg / 51% attack, consensus bug, wrong payouts, key compromise | Immediately |
| SEV2 | Node crash loop, spam flooding, seed node down, explorer wrong | Same day |
| SEV3 | Single user issue, cosmetic bug | Normal |

## Signals to watch

* Node log `Rejected chain: reorg of N blocks exceeds limit` → someone is trying a deep reorg (SEV1).
* Nodes disagreeing on the tip hash at the same height for more than ~10 blocks → split (SEV1).
* Hashrate or block rate jumping several times over; lots of 1/8-work "stall" blocks.
* Payout amounts that don't match `GET /payouts`.
* Many 429 / 503 responses in logs → flooding (SEV2).
* The healthcheck (`ops/healthcheck.py`) alert webhook firing. The URL lives only in env `NETLITHIC_ALERT_WEBHOOK` and is never posted publicly.

## First 30 minutes (SEV1)

1. **Confirm.** Compare `/status` on every node you run plus one independent node.
2. **Pause payouts if the money math is in doubt** (see below). Mining and transfers keep running.
3. **Tell people.** Pin a short public note: what we see, what we've paused, and that we'll update by a set time. Never post tokens, keys or webhook URLs.
4. **Tell exchanges/bridges to halt deposits** (see `CONFIRMATIONS.md`).
5. **Preserve evidence.** Copy `chain.json`, node logs and the competing chain (`ops/backup_chain.sh`). Don't delete anything.
6. **Write a timeline** as you go, with times in America/Denver.

## Playbooks

**Deep reorg / 51%.** Upgraded nodes refuse reorgs deeper than 720 blocks (~12 h at 60 s).
If an attacker reorged less than that, find the double-spent txs by comparing
the two chains, publish them, and tell affected users. If honest nodes ended up
on different sides of the limit, choose the honest chain, publish its hash at a
height as a checkpoint in `config/checkpoints.json`, ship it, and have operators
restart.

**Consensus bug.** Pause payouts. Reproduce it in a test. Fix it behind a new
fork height that is far enough ahead for everyone to upgrade, and never rewrite
blocks that are already final.

**Flooding.** Rate limits are on by default (`NETLITHIC_RATE_PER_SECOND`,
`NETLITHIC_RATE_BURST`, `NETLITHIC_MAX_MEMPOOL`). Tighten them with env vars and
block abusive IPs at the firewall (`ops/hardening/`).

**Key / token compromise.** Rotate the admin token (env
`NETLITHIC_ADMIN_TOKEN`), the Discord bot token and the webhook at their
providers. If the pause key leaked, publish a new `PAUSE_PUBKEY` in a release.
Because the switch can only stop payouts, a leaked pause key cannot steal
coins. The worst case is someone pausing or resuming, and that shows up in
the public log.

## The pause switch (design)

Goal: one person (the project key holder) can halt epoch payouts quickly while something is
investigated. Nothing hidden, no special power over coins.

* **On-chain and public.** A pause is a signed transaction in a block, so every
  pause and resume, with its reason, is permanent in the chain and visible to
  everyone at `GET /pause_status` (`paused`, `nonce`, full `log`).
* **Key-holder-only.** Only the key whose public key equals `pause.PAUSE_PUBKEY` can
  sign. The key is Ed25519, signed over the chain_id, and nonces 1, 2, 3...
  mean it can't be replayed.
* **Narrow.** While paused, `expected_payouts` returns nothing, so the community
  pool keeps the coins. Mining, rewards, transfers and balances are untouched.
  It cannot move coins or change rules.
* **Reason required** (1–280 characters).
* **Behind a flag twice:** consensus only accepts pause txs from height 5000
  (`SECURITY_FORK_HEIGHT`), and the whole feature is off while
  `PAUSE_PUBKEY = ""`, which is the default in this branch.
* **Operating it:** on an offline machine, `python ops/pause_tx.py pause "reason" --nonce N`
  (the key is typed at a hidden prompt), then POST the printed JSON to any
  node's `/submit_tx`. To resume, use `resume` with the next nonce. To keep a pause past 10,080 blocks (~7 days), send `renew` before it lapses.

### Pause key status
1. Generate the pause keypair offline ([PAUSE_KEY_CEREMONY.md](PAUSE_KEY_CEREMONY.md)) and set `PAUSE_PUBKEY` in `pause.py` before 5000.
2. No back pay. Skipped epochs stay in the pool (verified by test).
3. A pause expires automatically 10,080 blocks (~7 days at 60 s; set 2026-10-23, was 1008) after its last pause/renew tx,
   unless publicly renewed by another signed pause tx (logged as "renew"). Skipped payouts are
   never back-paid; the coins stay in the community pool, so the next paying epoch's earners split
   a larger pool (each still capped at 25k).

## After the incident

Write a public post-mortem within a week covering the timeline, root cause,
what changed, and how we'll catch it next time. Add a regression test.
