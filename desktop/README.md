# Netlithic desktop app (TEST NETWORK)

One window: make or bring back a wallet, run your own node, and press **Start helping**.
Test coins have no value.

## What it does on the user's PC
- Starts a node on 127.0.0.1:47101 that copies the test network from the public node.
- Shows the wallet at 127.0.0.1:47100 inside a Netlithic window (WebView2 on Windows).
  The secret key is made in the window, locked with the user's password and saved only
  in `%LOCALAPPDATA%\Netlithic\testnet\window`. The Python side only sees the address.
- **Start helping** mines blocks to the user's address on their own node, one try every
  60 seconds (`NETLITHIC_MINE_PACE`), only once the node has caught up.
- Free test coins come from the public site; explorer links open the public explorer
  in the normal browser.

## Try it without building (any OS)
    pip install cryptography pywebview
    python desktop/netlithic_desktop.py            # window
    python desktop/netlithic_desktop.py --no-window # then open http://127.0.0.1:47100

## Build the Windows installer
Needs Windows 10/11, Python 3.11+ (python.org) and Inno Setup 6 (jrsoftware.org).

    powershell -ExecutionPolicy Bypass -File desktop\build_windows.ps1

Result: `desktop\Output\Netlithic-Setup.exe` plus `Netlithic-Setup.exe.sha256` (its SHA-256 checksum).
It installs per user (no admin prompt). The build stops if any wallet or token file is found inside it.

Signing is optional and off unless `NETLITHIC_SIGN_CMD` (e.g. Azure Artifact Signing via signtool,
with `{file}` for the path) or `NETLITHIC_SIGN_PFX` is set. Unsigned builds are for testing only.

## Publishing rules (founder decides)
- Publish only signed installers, only on testnet.netlithicchain.com or the official GitHub release,
  together with the `.sha256` checksum. Users check it with `certutil -hashfile Netlithic-Setup.exe SHA256`.

## Known limits
- The installer is not code-signed yet (no certificate bought), so it must not be published.
  We never tell users to click past a Windows warning.
- Ports 47100/47101 are fixed so the saved wallet is found again; if another program uses
  them, the app can't start.
