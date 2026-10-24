# Netlithic test network

> **This is a test network. Test coins (test NLT) have no value.** You can't sell or swap them,
> and the test chain may be wiped and restarted at any time.

Netlithic is a blockchain owned by the people who help run it. This repository holds everything
you need to try the **test network**: the node, the wallet, a web wallet, a faucet that hands out
free test coins, and a block explorer.

## The easiest way to take part (no terminal)
Open the Netlithic web wallet in your browser, click **Create my wallet**, write down your backup
code, and click **Get free test coins**. That's it. Your secret key stays in your browser. See
[docs/WEB_WALLET.md](docs/WEB_WALLET.md).

## Run your own test node (Windows, no admin needed)
Install Python 3.10+ and Git, open a normal PowerShell, and paste these one at a time:

```powershell
git clone https://github.com/Netlithicchain/netlithic-testnet.git
```
```powershell
cd netlithic-testnet
```
```powershell
python easy_node.py install
```

Then check http://127.0.0.1:8000/status. The node does not update itself; to update, run `git pull`
and then `python easy_node.py install` again. To remove it, run `python easy_node.py uninstall`
(your data is kept). Full steps, plus the advanced manual setup with a wallet and mining, are in
[docs/JOIN_TESTNET.md](docs/JOIN_TESTNET.md).

## Words you'll see
- **Wallet**: your secret key plus an address. The address is like an account number; share it to receive coins.
- **Backup code**: the only way to get your wallet back. Write it down. Never share it.
- **Node**: a computer that keeps a copy of the chain and checks everyone's transfers.
- **Block**: a batch of transfers added to the chain about every 10 minutes.
- **Contribution points**: a record of how much you've helped the network.

## What's in here
| File | What it does |
|---|---|
| `netlithic.py` | the main command-line tool (wallets, node, sending, mining) |
| `node.py`, `p2p_http.py`, `core.py`, `economics.py`, `contributions.py` | the node and chain rules |
| `wallet.py` | key creation and transfer signing |
| `easy_node.py` | easy installer: runs a test node in the background (Windows, Mac, Linux) |
| `join_testnet.py` | one-command setup to join the test network |
| `webwallet.py`, `webwallet/` | the browser wallet |
| `faucet.py` | hands out free test coins, with daily limits |
| `explorer.py` | read-only web pages showing blocks, transfers and addresses |
| `config/genesis_config.json` | the shared network settings every node must use |
| `docs/ECONOMIC_DESIGN.md` | how supply, rewards and points work |
| `RUNNING.md` | all commands in detail |

Tests: `pip install -r requirements.txt pytest` then `python -m pytest`.

## Security
Found a problem? Please email security@netlithicchain.com and see [SECURITY.md](SECURITY.md).

## License
MIT. See [LICENSE](LICENSE).
