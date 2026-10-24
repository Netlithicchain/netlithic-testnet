# Points leaderboard (explorer /leaderboard)

Test coins have no value.

The page shows this notice under the heading: "Contribution points are recognition only. They have no dollar value, and there is no promise they convert to NLT. Test coins have no value." Its closing line reads: "Points decide each address's share of community test coins, which have no value."

- Page: `/explorer/leaderboard` (link in the explorer menu). `?short=1` shows shortened addresses.
- Shows the top 100 addresses by contribution points **this reward period**, with blocks with
  transfers and transfers sent. Addresses only, no names, no balances.
- Explains how points are earned and the fair-play rules: self-sends and empty blocks earn 0,
  transfers under 1 test NLT earn 0, each sender/recipient pair counts at most 10 times per period.
- Data comes from the node's new `GET /leaderboard` (added on the test-payouts branch; this
  branch is stacked on it). Locked community/treasury/reserve addresses are left out.

## Deploy
Deploy together with test-payouts (node A + node 2 for `/leaderboard`, then restart the explorer
service). Before the short-period fork at block 5000 the "period" is the long monthly one, so the board
covers everything since block 0 until block 5000. Read-only page; no chain impact; undo = redeploy previous explorer.py.
