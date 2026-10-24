# Share your address (web wallet + desktop app)

Test coins have no value.

## What it does
- **Copy address**: unchanged.
- **Share**: on phones (where `navigator.share` exists) opens the system share sheet with
  "Send me test NLT on Netlithic (test coins have no value): <address>" plus a share link.
  Where it isn't available (desktop browsers, the pywebview desktop app) it copies the share link and says "Link copied".
- **Show QR code**: draws a QR code of the share link on a `<canvas>`. Uses the vendored
  `webwallet/static/qrcode.js` (Kazuhiko Arase, MIT, qrcode-generator 1.4.4, no dependencies, no CDN).
- **Share link**: `<share base>/send?to=<address>`. Opening it shows the normal wallet. After unlock,
  the recipient field is filled in and a note says nothing is sent until "Yes, send it".
  The address must match `^[0-9a-f]{32}$`; anything else is ignored. The amount is never filled, nothing
  is reviewed or sent automatically, and the URL is reset to `/` afterwards.

## Share base
`/api/info` now returns `share_base`. Set with `--share-base` / `NETLITHIC_WALLET_SHARE_BASE`
(must be a plain `https://host[/path]`; anything else is refused at startup). Empty = the page's own address.
The desktop app passes `PUBLIC_SITE` (https://testnet.netlithicchain.com), so its links never point at 127.0.0.1.
On the server, set `NETLITHIC_WALLET_SHARE_BASE=https://testnet.netlithicchain.com` (or leave empty,
since the public page's own origin is already correct).

## Security
No change to the API rules: the server still validates every address itself and only forwards signed
transfers. CSP unchanged (`script-src 'self'`); `/send` serves the same index.html; `/qrcode.js` is
the only new file served.

## Deploy (only with the founder's approval)
Copy `webwallet.py` and `webwallet/static/` to /opt/netlithic on testnet-1 (and node 2 for sync),
restart `netlithic-webwallet`. Desktop users get it with the next desktop build. Undo: restore the backup.
