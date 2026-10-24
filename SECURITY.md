# Security policy

## Reporting a problem
Email **security@netlithicchain.com**. *(This mailbox is being set up; until it is live, open a
private GitHub security advisory on this repo instead.)* Please don't open a public issue for a
security bug.

Include what you found, how to reproduce it, and what you think it affects. We'll confirm we got
it within 3 business days.

## Coordinated disclosure
We follow a **90-day** coordinated disclosure window: please give us up to 90 days from your
report to ship a fix before you publish. If a fix ships sooner, we'll agree a publish date with
you, and we'll credit you if you'd like.

## Scope
In scope: chain rules and validation (`core.py`, `economics.py`, `contributions.py`, `wallet.py`),
the node (`p2p_http.py`), the web wallet, the desktop app and installer, the explorer, and the
founder relay script (`ops/`). See `docs/THREAT_MODEL.md`.

Out of scope: social engineering, physical attacks, attacks needing a compromised user device,
volumetric DDoS against our servers, and third-party services we don't run.

Test on your own local node or the public testnet. Don't degrade the testnet for others or
access anyone else's wallet.

## Testnet coins
Netlithic is a test network. **Test coins have no value** and can't be sold or redeemed.

## We pay for real bugs
If you report a real, previously unknown security bug in scope, we'll thank you with a reward
paid in **US dollars** from project revenue or the founder's own funds. It is **never** paid from
the chain's treasury or any on-chain pool. Amounts are decided case by case; there is no fixed
schedule yet.
