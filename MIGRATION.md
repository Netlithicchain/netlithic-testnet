# Migrations

## Chain id from config, fees, no printed keys (branch `testnet-prep`)

This is a new chain again (the fee setting is in the genesis hash, and every transfer now has a
signed `fee` field). Back up, then run `python netlithic.py genesis --yes` on every node.
- Old transfers without a `fee` are rejected. Old reward entries without `"fees"` are read as 0.
- `chain_id` now comes only from `config/genesis_config.json`. All nodes must use the same file.
  Nodes refuse peers, blocks and transfers from another `chain_id`.
- `wallet show --show-private` is gone. Use `wallet show <name> --export-key <new file>`; the key
  goes to an owner-only file and is never shown on screen. `python wallet.py` prints only the address.
- Existing `wallets.json` files keep working. The next time a wallet is created, the file is
  rewritten as owner-only (0600). On Windows, keep it on the encrypted drive.
- `send` now pays 0.0001 NLT by default; `--fee` pays more.

## Multisig and point weights (branch `multisig`)

The multisig keyholders and point weights are part of the genesis settings hash, so this is a new
chain again: back up, then run `python netlithic.py genesis --yes` on every node. Wallets are not affected.

Treasury and reserve spending stays off (`mode: "none"`). Nothing to configure now.

Later, when independent contributors join and multisig should be turned on:
1. Each keyholder runs `python netlithic.py wallet new <name>` on their own machine and sends only
   their **public key** (from `wallet show <name>`, never the private key or an `--export-key` file).
2. Put those keys into `treasury_spend_approval` with `"mode": "multisig"` and a threshold. At least
   two keyholders must not be the founder; mark his own entry `"founder": true`.
3. Reset genesis on all nodes, because this changes the chain.

## Time locks and community payouts (branch `timelocks`)

The genesis block now also records a hash of all the economic settings (unlock schedules, epoch
length, cap, vesting, approval mode). That makes it a new chain again, so run
`python netlithic.py genesis --yes` on every node after backing up. Wallets are not affected.
Changing any of those settings later also means a new genesis.

## Supply cap and genesis allocations (branch `supply`)

- The genesis block now contains the locked community, treasury and reserve allocations, so every
  chain made before this change is a different chain. Nodes will refuse to load the old `chain.json`.
- Amounts are now in base units (1 NLT = 10^8). Old balances don't carry over in any case.
- Wallets are **not** affected. Keep them.

Steps (in E:\Netlithic, after backing up `chain.json` and `state_contributions.json`):
1. Stop the nodes and bring in the new code.
2. `python netlithic.py genesis --yes` (this backs up the old files and writes the new genesis).
3. `python netlithic.py node start 8000`, then `python netlithic.py status`. You should see
   `"minted": "800000000"` in the supply section.
4. Do the same on every node. All nodes must use the same `config/genesis_config.json`.

Done: `config/genesis_config.json` now has the real community, treasury and reserve addresses
(`"placeholder": false`). If you ever change them, reset genesis on all nodes again.

# Migrating to Ed25519 signatures

## What changed

- Wallets now use real Ed25519 keys. Before, the "signature" was a SHA-256
  hash of public data, so anyone could fake a transfer from any wallet that
  had ever sent one. That is no longer possible.
- Every transaction now carries `"chain_id": "netlithic-dev-1"` and is signed
  over the whole transaction (except the signature itself).
- Addresses still look the same (32 hex characters, derived from the public
  key), but every **new** wallet has a new address.
- Netlithic now needs one package: `pip install -r requirements.txt`
  (installs `cryptography`).
- A node will no longer silently overwrite a `chain.json` it can't validate.
  It stops with an error instead.

## What this means for you

Old wallets and the old chain can't be carried over:

- Old wallets can't produce valid signatures. The code refuses to load them.
- The old `chain.json` has old-style signatures in it, so it no longer
  validates. Old balances and contribution points don't move to the new chain.

So the plan is: keep backups, make new wallets, start a fresh chain.

## Steps on E:\Netlithic (Windows, run in that folder)

1. Stop any running nodes.
2. Back up everything first (keep this folder somewhere safe and private;
   it contains your old private keys):
   ```
   mkdir E:\Netlithic-backup-pre-ed25519
   copy E:\Netlithic\wallets.json E:\Netlithic-backup-pre-ed25519\
   copy E:\Netlithic\chain.json E:\Netlithic-backup-pre-ed25519\
   copy E:\Netlithic\state_contributions.json E:\Netlithic-backup-pre-ed25519\
   copy E:\Netlithic\mempool.json E:\Netlithic-backup-pre-ed25519\
   ```
3. Bring in the new code (merge the `ed25519` branch), then install the dependency:
   ```
   cd E:\Netlithic
   python -m pip install -r requirements.txt
   ```
4. Move the old wallets file aside so new wallets start clean:
   ```
   ren wallets.json wallets.pre-ed25519.json
   ```
5. Generate new wallets (repeat per wallet you want):
   ```
   python netlithic.py wallet new <name>
   ```
   Note the address it prints. The private key is saved to `wallets.json`
   and is not printed. Never share that file.
6. Reset the chain:
   ```
   python netlithic.py genesis --yes
   ```
   (same as `python new_genesis.py --yes`)
   This renames `chain.json`, `mempool.json` and `state_contributions.json`
   to `*.pre-ed25519-<time>.bak` and creates a fresh genesis chain. It does
   not touch `wallets.json` or `peers.json`.
7. Start the node and check it: `python netlithic.py node start 8000`, then open
   http://127.0.0.1:8000/status. Height should be 0.
8. Do the same on every other node, because all nodes must restart from the new genesis.

To send a transfer now: `python netlithic.py send <from> <to> <amount> [--node URL]`.

# Migrating to token-only admin (`admin-token` branch)

- `/mine` and `/add_peer` no longer trust localhost. Every call needs `X-Admin-Token` matching
  `NETLITHIC_ADMIN_TOKEN`. With no token set they return 403.
- Local dev without a token: set `NETLITHIC_DEV_LOCAL_ADMIN=1` (works only on `netlithic-dev*` chains,
  from this machine).
- `python netlithic.py mine` sends the token from `NETLITHIC_ADMIN_TOKEN` or `--admin-token`.
- Any script that called `/mine` on localhost must now add the header.
- Chain data, wallets and genesis are unchanged: no reset needed.
