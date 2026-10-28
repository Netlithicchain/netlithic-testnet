# Founder mining page (explorer /founder)

Test coins have no value.

- Page: `/explorer/founder` (link "Founder" in the explorer menu).
- Shows every block whose reward went to the founder/faucet address
  `e924523b2614a39b9bb4ff8bfc05e747`: height, time, reward, and kind
  ("Normal (before 5000)" or "Backstop").
- Cards: blocks mined, total mined, backstop blocks from block 5000, and "Backstop rule followed: yes/no".
- States the rule from ops/founder_miner.py: from block 5000 the server only
  relays; it may make one block only if nobody has made a block for 30 minutes, at most 1 per 30 minutes.

## How the check works
`founder_report()` in explorer.py reads `/chain` (no node change). For each founder block with
index >= 5000 it fails the check if it came less than 30 min after the previous block (from anyone),
or less than 30 min after the previous founder backstop block. Any failure is listed on the page.
Block index >= 5000 = backstop matches `decide()` in ops/founder_miner.py (mines while `height + 1 < 5000`).

## Deploy (only with the founder's approval)
Copy explorer.py to the explorer host and restart `netlithic-explorer`. Read-only page, no chain
impact. Undo: redeploy the previous explorer.py.
