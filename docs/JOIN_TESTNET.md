# Join the Netlithic testnet

> **Testnet only. Test NLT has no value.** You can't sell it or swap it, and the
> chain can be wiped at any reset. Please don't treat it as money.

## Easy way: run a test node on Windows (no admin needed)

This runs a node that keeps a copy of the test network on your computer. It does
not create a wallet, and nothing private leaves your computer. You never need
"Run as administrator".

### Before you start (one time)
1. Install **Python 3.10 or newer** from python.org. During setup, tick **"Add python.exe to PATH"**.
2. Install **Git** from git-scm.com (the default choices are fine).
3. Open a normal PowerShell: click Start, type `PowerShell`, press Enter.

**Paste each command below on its own, press Enter, and wait for it to finish
before you paste the next one.** Pasting two lines at once can join them and
break them.

### Install

Step 1. Download the files:
```powershell
git clone https://github.com/Netlithicchain/netlithic-testnet.git
```

Step 2. Go into the folder:
```powershell
cd netlithic-testnet
```

Step 3. Install and start your node (about a minute the first time):
```powershell
python easy_node.py install
```
If Windows lets it, the node is added as a background task. If Windows says no,
which is common on normal accounts, it uses your Startup folder instead and
says so in a plain sentence. Both are fine. Wait until it prints **Done**. If
you see a line starting with `ERROR:`, take a screenshot of the window and send
it to us.

Step 4. Check that it works:
```powershell
Invoke-RestMethod http://127.0.0.1:8000/status
```
You can also open http://127.0.0.1:8000/status in a web browser. The `height`
number is how many blocks your node has. It goes up by itself, but blocks can
take several minutes each, so give it time. You can close PowerShell. The node
keeps running in the background and starts again each time you log in.

### Update

**The node does not update itself.** When we announce a new version, update by hand.
Your node's data is kept.

Open a normal PowerShell, then paste each command on its own:
```powershell
cd netlithic-testnet
```
If PowerShell can't find the folder, use `cd $HOME\netlithic-testnet` instead.
```powershell
git pull
```
```powershell
python easy_node.py install
```
Wait for **Done**, then run the status check again and look at the `version` line.

### Uninstall

From inside the `netlithic-testnet` folder:
```powershell
python easy_node.py uninstall
```
It should end with "Service removed". Your node's data is kept in
`%LOCALAPPDATA%\Netlithic\node`. You can delete that folder yourself if you want.

### If something goes wrong
- **"python is not recognized" or "git is not recognized".** Reinstall it (tick "Add python.exe to PATH" for Python), close PowerShell, open a new one and try again.
- **"can't open file ... easy_node.py".** You are not inside the folder. Run the `cd` command again.
- **The status check can't connect.** Wait a minute and try again. If it still fails, run the install command again.
- **Anything else.** Take a screenshot of the whole PowerShell window and send it to us. Please don't guess at fixes.

Other systems (Mac, Linux, Docker) are covered in [EASY_NODE.md](EASY_NODE.md).

---

## Advanced: manual setup (wallet, mining, options)

Use this if you are comfortable with a terminal and want your own wallet and
mining on your own node. It is separate from the easy way above, so don't run
both on the same port.

The seed node is `http://137.184.199.238:8000`. You can point to a different one
with `--seed URL` or the `NETLITHIC_SEED_URL` environment variable.

### What you need
- Python 3.10 or newer, plus `git` (or the code as a zip)
- About 100 MB of disk space

### One-command setup

**Windows (PowerShell)**
```powershell
git clone https://github.com/Netlithicchain/netlithic-testnet.git netlithic
cd netlithic
py join_testnet.py
```

**Linux / macOS**
```bash
git clone https://github.com/Netlithicchain/netlithic-testnet.git netlithic
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

#### Options
| Option | Meaning |
|---|---|
| `--seed URL` | seed node to join (default above) |
| `--port N` | your node's port (default 8000) |
| `--host 0.0.0.0` | accept connections from other machines |
| `--advertise-url http://YOUR_PUBLIC_IP:8000` | the URL the seed should use to reach you. The script then calls the seed's `/hello`, and the seed checks that URL before adding you. |
| `--data-dir DIR` | where your chain, wallet and token live (default `testnet-node`) |
| `--wallet-name NAME` | name for your wallet (default `me`) |

Without `--advertise-url`, which is the normal case behind a home router, your node still
downloads the chain and sends the blocks you mine to the seed. Other nodes just
can't connect to you.

### Mine test blocks on your node

In the commands below, replace `YOUR_ADDRESS` with the address the setup script printed.

**Windows (PowerShell)**, in a second window inside the `netlithic` folder:
```powershell
.\.venv-testnet\Scripts\Activate.ps1
$env:NETLITHIC_ADMIN_TOKEN = Get-Content testnet-node\admin_token.txt
python netlithic.py mine YOUR_ADDRESS --node http://127.0.0.1:8000
python netlithic.py balance YOUR_ADDRESS --node http://127.0.0.1:8000
```

**Linux / macOS**
```bash
source .venv-testnet/bin/activate
export NETLITHIC_ADMIN_TOKEN="$(cat testnet-node/admin_token.txt)"
python netlithic.py mine YOUR_ADDRESS --node http://127.0.0.1:8000
python netlithic.py balance YOUR_ADDRESS --node http://127.0.0.1:8000
```

Each block pays a test reward and gives you contribution points. Your node
shares the block with the seed. Use your own token on your own node only; the
seed's token stays with the seed's operator.

### Send a test transfer

Replace `RECIPIENT_ADDRESS` with the address you are sending to.
```bash
python netlithic.py send me RECIPIENT_ADDRESS 1 --wallets testnet-node/wallets.json --node http://127.0.0.1:8000
```
Need coins without mining? Use the testnet faucet once it's online. It gives out
10 test NLT per address per day.

### Safety
- Never paste your `wallets.json` or `admin_token.txt` anywhere. Nobody from Netlithic will ask for them.
- The script refuses to run if the seed is not on a `netlithic-dev` test chain
  or if its chain_id doesn't match your code. Run `git pull` if it complains.
- Logs go to `testnet-node/node.log`.
