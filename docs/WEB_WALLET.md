# Netlithic web wallet (test network)

A wallet that runs in the browser. No terminal, no installs.

## For people using it
1. Open the wallet page and click **Create my wallet**.
2. Pick a password. It locks the wallet on this device.
3. Write down the backup code (17 groups of 4). It's the only way back in if this device is lost.
4. Click **Get free test coins**, then send some to a friend.

Test coins have no value. It's all for practice.

## How it keeps your key safe
- The secret key is made in your browser and stored there, locked with your password
  (PBKDF2-SHA256, 600,000 rounds, then AES-256-GCM). It is never sent to any server.
- Transfers are signed in the browser with Ed25519 (vendored tweetnacl 1.0.3, public domain,
  `webwallet/static/LICENSE-tweetnacl.txt`), in the exact same format as `wallet.py`.
- The page loads nothing from other websites. A strict Content-Security-Policy allows only
  its own scripts, styles and images, and all text is written as plain text.
- The backup file is the same locked format, so it's safe to keep in cloud storage
  as long as the password is strong.

## Running it (operators)
    python webwallet.py --node http://127.0.0.1:8000 --faucet http://127.0.0.1:8090 --port 8082

`webwallet.py` serves the page and a small same-origin API, so the node and faucet need
**no CORS changes** and stay unreachable from other websites' scripts:

| Wallet API | Goes to | Notes |
|---|---|---|
| `GET /api/info` | node `/status` | chain id + minimum fee; refuses if chain ids differ |
| `GET /api/account?address=` | node `/account`, `/contributions` | balance, next nonce, points |
| `POST /api/send` | node `/receive_tx` | only forwarded if the signature checks out |
| `POST /api/drip` | faucet `/api/drip` | passes the visitor IP in `X-Forwarded-For` |

Admin endpoints (`/mine`, `/add_peer`) are never proxied.

**HTTPS is required for real users.** Browsers only allow the crypto the wallet needs on
HTTPS pages (or `localhost`). Put a TLS reverse proxy (e.g. Caddy with a domain name) in
front, run `webwallet.py --trust-proxy` behind it, and run the faucet with `--trust-proxy`
bound to localhost only, so per-visitor faucet limits see the real visitor address.
