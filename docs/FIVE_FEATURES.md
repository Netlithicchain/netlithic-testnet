# Five features for everyday users

Test coins have no value. They are only for practice on the test network.

## 1. Wallet backup
- "Save your backup" takes three steps: (1) download a backup file, locked with a password (PBKDF2 600k + AES-GCM); (2) optionally print a backup page; the print styles show only that sheet, and the code is wiped from the page after printing; (3) check the backup: pick the file and type its password, or type the backup code. The check runs on this device only and compares the address it gets back.
- A reminder banner stays on until a check passes. Restoring a wallet also counts as confirmed (`netlithic.backup.confirmed.v1.<address>`).
- The secret key never goes to the server and is never logged.

## 2. Activity history
- New read-only node endpoint `GET /history?address=&offset=&limit=` (`history.py`), which lists mined blocks, reward shares, allocations, sent and received transfers, newest first, up to 50 per page.
- The web wallet proxies `GET /api/history` and checks every field from the node. On the first page it adds pending entries from the mempool. "Show older" loads the next page.
- **Needs a node deploy.** Older nodes answer 404, and the wallet then says history isn't available yet.

## 3. Address book
- Contacts are saved only in this browser (`netlithic.contacts.v1`) and never sent to the server.
- They can be picked in Send, and their names show in the review and in Activity. The `/send?to=` prefill has an "Add to contacts" button.

## 4. Desktop helping stats
- `<data dir>/helping-stats.json` holds time spent helping today and in total, blocks found (all time), and the last 60 days. Gaps over 120 s are not counted. Saves are atomic, and a broken file starts the stats over.
- The earn card also shows points and blocks until the next reward share.

## 5. Notifications (opt-in)
- Off by default. Turning them on asks first, then asks the browser for permission.
- Desktop: an OS notification (osascript / notify-send), or an in-app message where those aren't available. Browser: the Notification API when the tab is hidden, an in-app message otherwise.
- The first load stays quiet, and pending items never notify. Every message says test coins have no value.
