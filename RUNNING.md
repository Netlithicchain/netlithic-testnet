# Running Netlithic

```
pip install -r requirements.txt
python netlithic.py --help
```

Everything goes through one tool, `netlithic.py`. Each command also has `--help`.
The default port is 8000, and the default node is `http://127.0.0.1:8000`.

## Wallets

```
python netlithic.py wallet new alice          # creates an Ed25519 wallet in ./wallets.json
python netlithic.py wallet list
python netlithic.py wallet show alice         # name, address, pubkey; no private key
python netlithic.py wallet show alice --export-key alice.key   # backs up the key to a NEW 0600 file; never printed
```

`--wallets other.json` uses a different wallet file. Keep wallet files private.
`wallets.json` is written owner-only (0600). No command or script prints a private key;
`python wallet.py <name>` also creates a wallet and prints only its address.

## Nodes

```
python netlithic.py genesis --data-dir nodeA --yes      # fresh chain (backs up old files)
python netlithic.py node start 8000 --data-dir nodeA
python netlithic.py node start 8001 --data-dir nodeB --peer http://127.0.0.1:8000
```

- `--data-dir` is the folder where the node keeps `chain.json`, `mempool.json`, `peers.json` and
  `state_contributions.json`. It's created if missing. Use one folder per node.
- `--peer URL` connects to a peer at startup (repeatable).
- `--host` is the address to listen on (default `127.0.0.1`, this machine only).
- `--advertise-url URL` is the address other machines should use to reach this node, for example
  `http://203.0.113.5:8000`. You need it across machines: without it the node calls itself
  `http://127.0.0.1:<port>`.
- `python node.py ...` takes the same options.

## Using a node

```
python netlithic.py status
python netlithic.py mine alice                  # mines one block, reward to alice
python netlithic.py send alice <address> 5      # signs locally, submits to the node (fee 0.0001 NLT)
python netlithic.py send alice <address> 5 --fee 0.001   # pay a higher fee to be picked first
python netlithic.py balance <address>
python netlithic.py points                      # contribution points, from the chain
python netlithic.py validate --data-dir nodeA   # checks chain.json without changing it
```

All of these take `--node URL`. `send` and `mine` also accept a wallet name instead of an address.

## Supply and units

- Amounts on the chain are whole numbers in base units: 1 NLT = 100,000,000 base units (8 decimals).
  The CLI takes and shows NLT (`send alice <addr> 0.25`). `/balance` returns both `balance`
  (base units) and `balance_nlt`.
- The max supply is 1,000,000,000 NLT, fixed in code (`economics.py`).
- Genesis mints 600M community, 150M treasury and 50M reserve to the addresses in
  `config/genesis_config.json`. Those three addresses are locked: no transfer from them is accepted.
- Miners get 10 NLT per block, halving every 10,000,000 blocks, paid from the 200M miner bucket and
  capped at what's left in it.
- Nodes refuse a chain whose genesis doesn't match the config, or that mints more than the max supply.
- `/status` has a `supply` section: minted, remaining, miner rewards paid and remaining, and the next
  block reward. `validate` prints the minted total.
- **The genesis addresses are real** (`"placeholder": false`): community `f953a954...`, treasury
  `e26742c7...`, reserve `65ca7858...`. Their keys are held offline by the founder, never on a node. Every
  node must use the same config file, because changing it changes the genesis block, which makes
  a different chain.

## Time locks, community payouts and vesting

- **Time locks:** the community, treasury and reserve allocations unlock in monthly steps by block
  height. The schedules are proposals in `config/genesis_config.json`. Nothing past the unlocked
  amount can be spent.
- **Treasury and reserve:** no spends at all by default (`treasury_spend_approval.mode: "none"`).
  Multisig (see below) is built but off.
- **Community payouts:** the community pool only pays out through protocol payouts. Every epoch block
  (default 175,200 blocks) pays the unlocked pool to contributors, pro rata by points earned in that
  epoch, capped per address (default 25,000 NLT). The excess stays in the pool.
- **Vesting:** payouts vest linearly (default 6 epochs). The unvested part can't be spent.
- `python netlithic.py economy` shows each bucket (allocation, unlocked, locked, balance) and the next
  payout. On the epoch's last block before payout, it also previews who gets what.
- `balance` and `/balance` show `spendable_nlt` and `locked_or_unvested_nlt`.
- **Local testing with small numbers:** copy the config, change `time.blocks_per_month` and
  `community_payouts.epoch_blocks` (for example 10 and 5), and point every node and CLI call at it
  with `NETLITHIC_GENESIS_CONFIG=/path/to/test_config.json`. All of these numbers are hashed into
  genesis, so nodes with different settings are on different chains.

## Treasury and reserve multisig spends

**Off by default:** the mode is `"none"`, no keyholders are listed, and no spend is possible.
Multisig is ready for when independent contributors join. To turn it on, set this in
`config/genesis_config.json` (it changes genesis):

```
"treasury_spend_approval": {"mode": "multisig", "threshold": 3,
  "keyholders": [{"name": "...", "pubkey": "<64-hex Ed25519 public key>", "founder": false}, ...]}
```

When on, a spend needs `threshold` distinct keyholder signatures. It can't move more than the
bucket's unlocked amount, and it carries the chain id and the bucket's next nonce, so it can't be
replayed. At least two keyholders must not be the founder (mark his own entry `"founder": true`).

How a spend works, using a file passed between keyholders:

```
python netlithic.py multisig build treasury <recipient> 1000 --file spend.json   # anyone
python netlithic.py multisig sign k1 --file spend.json      # each keyholder, on their own machine
python netlithic.py multisig sign k3 --file spend.json
python netlithic.py multisig sign k5 --file spend.json
python netlithic.py multisig submit --file spend.json       # then it's mined like any tx
```

`GET /multisig` shows the mode, threshold, keyholders and next nonce for each bucket.

For local tests, a config with `chain_id` starting `netlithic-dev` may list keyholders with
`"test_key": true`, or use `"mode": "single_key_dev"`. Both are refused on any other chain id.

## Contribution points

Points are computed by replaying the chain. They are stored in tenths: by default a mined block
earns 2.5 (0 if the block is empty or only has miner-to-self transfers), and the sender of each
transfer earns 1. `state_contributions.json` is only a cache. It's rebuilt when the node loads
the chain and on every new block or chain switch, so it can't drift. See them with
`GET /contributions` (sorted, highest first) or `python netlithic.py points`. `/status` also
shows the number of contributors.

## Admin endpoints: `/mine` and `/add_peer`

Every request to these two endpoints must send the header `X-Admin-Token`, matching the
`NETLITHIC_ADMIN_TOKEN` environment variable the node was started with. This applies to requests
from this machine too: being on localhost is **not** enough (so a local proxy or gateway can't grant
admin by accident). The token is compared in constant time. A wrong or missing token gets HTTP 403.

If `NETLITHIC_ADMIN_TOKEN` is not set, the admin endpoints are switched off (403). For local
development only, you can set `NETLITHIC_DEV_LOCAL_ADMIN=1`: then, on a chain whose `chain_id`
starts with `netlithic-dev`, requests from this machine work without a token. It is ignored on any
other chain and for remote callers.

The CLI sends the token for you:

```
$env:NETLITHIC_ADMIN_TOKEN = "<token>"        # PowerShell; bash: export NETLITHIC_ADMIN_TOKEN=...
python netlithic.py mine alice
python netlithic.py mine alice --admin-token <token>   # overrides the env var
```

Prefer the env var: command-line arguments can show up in process lists.

## Peer lists

`/peers` and `/status` hide loopback peers (`127.x`, `::1`, `localhost`) from callers that are not
on this machine, since those URLs mean nothing elsewhere. Local callers still see every peer.

## Peer handshake: `/hello`

When you add a peer, your node also asks that peer to add it back by calling the peer's
`/hello` with your URL. `/hello` needs no token. The receiving node first calls
`<your URL>/status`, and only adds you if that answers and names exactly that URL. The same
URL rules and peer cap apply. Admin access is still only needed for `/add_peer` and `/mine`.

## Network id (`chain_id`)

`chain_id` in `config/genesis_config.json` is the only place the network id is set. Wallets sign
with it, nodes verify against it, and it is part of the genesis hash. `/status` and `/chain`
report it. A node refuses `/hello`, `/receive_tx`, `/receive_block` and `/submit_tx` messages that
carry a different `chain_id`, refuses to add a peer whose `/status` reports a different one, and
never syncs a chain from such a peer. Dev-only features (test keys, `single_key_dev`) still only
work when the id starts with `netlithic-dev`.

## Transaction fees (proposal: minimum 0.0001 NLT)

Every transfer carries a signed `fee` in base units. The minimum is `fees.min_fee_base_units`
in the genesis config (10000 = 0.0001 NLT). The sender pays amount + fee. The block's miner gets
all fees in the reward transaction (`"fees"` field) on top of the block reward. Fees move existing
coins, so they never add to the 1B supply. Miners fill blocks (max 1000 transfers) highest fee first.

## Limits

- Peers: only `http://` or `https://` URLs with a host and an optional port. No credentials,
  path or query. A node can't add itself, and it keeps at most 32 peers (`MAX_PEERS`).
- A block whose timestamp is more than 120 seconds ahead of this node's clock is rejected
  (`MAX_FUTURE_BLOCK_SECONDS`).
- POST bodies over 1 MB get HTTP 413. Responses from peers (for example `/chain`) over 16 MB
  are ignored.
- A rejected block comes back with a `reason` (for example `"insufficient proof of work"`),
  and the reason is logged as a warning.
