# Community rewards on the test network (test-payouts)

Test coins have no value. Nothing here is a promise of future coins.

## In plain words
- People earn contribution points by helping: mining blocks that carry transfers, and sending real transfers.
- At the end of each reward period ("epoch"), the network shares out part of the community pot,
  split by points. Nobody decides who gets what: every node works it out from the chain the same way.
- Each address gets at most 25,000 test NLT per period. Anything left over stays in the pot.
- A share unlocks bit by bit over the next 6 periods. The unlocked part is already in your
  balance, so there is no "claim" button.
- The wallet, the desktop app and the explorer now show: points this period, earned so far,
  still unlocking, and ready to use.

## Short periods on the test network only
A real period is 175,200 blocks (about a month at 15 s blocks), far too long to demo.
So the test network gets short periods starting at a fixed block (the "fork height").

- Rule: `economics.TESTNET_SHORT_EPOCHS = {"netlithic-dev-1": (5000, 1440)}` (the founder chose 144 on 2026-10-22; changed to 1440 = ~1 day at 60 s on 2026-10-23).
- It is a rule in the code, not a field of the genesis settings, so the genesis economics
  hash and the genesis block do **not** change (tested). The live chain keeps its history.
- It only applies to chain ids starting `netlithic-dev`. Any other chain id (a future mainnet)
  ignores it and keeps 175,200-block periods (tested).
- Before block 5000 nothing changes. From block 5000 on, a period is 1440 blocks; the first
  payout is at block **6440**, for points earned in blocks 5000-6439 (then 7880, 9320, ...).
  At 60 s blocks that is ~1 day after 5000; if only the founder backstop mines (1 block / 30 min) it is ~30 days. Points from before 5000
  are not paid out (they never reached the end of a full monthly period).
- Vesting = 6 short periods (864 blocks). Cap stays 25,000 per address per period.
- The pair cap (10 counted transfers per sender->recipient per period) now resets each short period.

## This is a fork: every node must upgrade before block 5000
A node without this code rejects the first short-period payout block (tested), so it would
split off. All nodes (node A, node 2, and any community node / desktop app) must run the new
code before the fork height. A restart of each node is needed; no chain reset.

## API
- `GET /payouts?address=<addr>`: earned_nlt, vesting_nlt, claimable_nlt, points_this_epoch,
  next_payout_height, blocks_until_next_payout, payout history.
- `GET /payouts` (no address): period overview (same as `community_payouts` in `/status`).
- `GET /account?address=` now includes a `payouts` summary.
- `GET /leaderboard`: points this period (used by the explorer leaderboard).
- `/status` -> `community_payouts` now has `current_epoch` and `testnet_short_epoch`.

## Deploy plan (needs the founder's go-ahead; nothing deployed)
1. The founder confirms the fork height and period length (confirmed: 5000 / 1440, same height the founder server stops mining and the difficulty fork activates). The fork height must
   be comfortably above the height on deploy day (chain was at 230 on 2026-10-22, about 6 blocks/hour).
2. Back up chain.json on both servers (`ops/backup_chain.sh`).
3. Deploy the branch to node 2 first, restart, check `/status` matches node A's height/hash.
4. Deploy to node A, the web wallet and the explorer; restart their services.
5. Check `/status` -> `community_payouts.testnet_short_epoch` on both nodes shows `{5000, 1440}`.
6. Ship a desktop build with the same code before block 5000 (old desktop nodes would split off).
7. At block 6440 check both nodes have the same block hash and `/payouts?address=` shows amounts.
Undo: before block 5000, roll back the code and restart; nothing on chain has changed.
After 5000, rolling back needs all nodes rolled back together.

## Note on pace
After block 5000 the founder server stops mining, so the speed of periods depends on community
miners. 1440 blocks is about a day at 60-second blocks.
