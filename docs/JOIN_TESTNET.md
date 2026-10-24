# Join the Netlithic testnet

> **Testnet only. Test NLT has no value.** You can't sell it or swap it, and the
> chain can be wiped at any reset. Please don't treat it as money.

The seed node is `http://137.184.199.238:8000`. You can point to a different one
with `--seed URL` or the `NETLITHIC_SEED_URL` environment variable.

## What you need
- Python 3.10 or newer, plus `git` (or the code as a zip)
- About 100 MB of disk space

## One-command setup

**Windows (PowerShell)**
```powershell
git clone <netlithic repo url> netlithic
cd netlithic
py join_testnet.py
```

**Linux / macOS**
```bash
git clone <netlithic repo url> netlithic
cd netlithic
python3 join_testnet.py
```

The script:
1. creates a virtualenv (`.venv-testnet`) and installs `cryptography`
2. creates **your own wallet** in `testnet-node/wallets.json`, readable only by you.
   It prints only the **address**. The private key never leaves that file.
3. creates an **admin token for your own node** in `testnet-node/admin_token.txt`,
   also owner-only and never printed. Only someone with this token can mine on your node.
4. starts your node on port 8000 with the shared genesis config, adds the seed
   as a peer and syncs the chain
5. prints the commands to mine on your node

Leave that window open, since the node keeps running there. Ctrl+C stops it. Run
the same command again later and it reuses your wallet and token.

### Options
| Option | Meaning |
|---|---|
| `--seed URL` | seed node to join (default above) |
| `--port N` | your node's port (default 8000) |
| `--host 0.0.0.0` | accept connections from other machines |
| `--advertise-url http://YOUR.PUBLIC.IP:8000` | the URL the seed should use to reach you. The script then calls the seed's `/hello`, and the seed checks that URL before adding you. |
| `--data-dir DIR` | where your chain, wallet and token live (default `testnet-node`) |
| `--wallet-name NAME` | name for your wallet (default `me`) |

Without `--advertise-url`, which is the normal case behind a home router, your node still
downloads the chain and sends the blocks you mine to the seed. Other nodes just
can't connect to you.

## Mine test blocks on your node

**Windows (PowerShell)**, in a second window inside the `netlithic` folder:
```powershell
.\.venv-testnet\Scripts\Activate.ps1
$env:NETLITHIC_ADMIN_TOKEN = Get-Content testnet-node\admin_token.txt
python netlithic.py mine <your address> --node http://127.0.0.1:8000
python netlithic.py balance <your address> --node http://127.0.0.1:8000
```

**Linux / macOS**
```bash
source .venv-testnet/bin/activate
export NETLITHIC_ADMIN_TOKEN="$(cat testnet-node/admin_token.txt)"
python netlithic.py mine <your address> --node http://127.0.0.1:8000
python netlithic.py balance <your address> --node http://127.0.0.1:8000
```

Each block pays a test reward and gives you contribution points. Your node
shares the block with the seed. Use your own token on your own node only; the
seed's token stays with the seed's operator.

## Send a test transfer
```bash
python netlithic.py send me <recipient address> 1 --wallets testnet-node/wallets.json --node http://127.0.0.1:8000
```
Need coins without mining? Use the testnet faucet once it's online. It gives out
10 test NLT per address per day.

## Safety
- Never paste your `wallets.json` or `admin_token.txt` anywhere. Nobody from Netlithic will ask for them.
- The script refuses to run if the seed is not on a `netlithic-dev` test chain
  or if its chain_id doesn't match your code. Run `git pull` if it complains.
- Logs go to `testnet-node/node.log`.
