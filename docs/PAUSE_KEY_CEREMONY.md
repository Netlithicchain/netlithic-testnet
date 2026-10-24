# Pause key ceremony (for Aric, do this yourself)

The pause switch only works if `PAUSE_PUBKEY` is set. The matching **private key**
must exist only on paper or metal and on a computer that never goes online.
Nobody else, including bots and helpers, should ever see it. You hand over only
the **public key**.

## You need

- An old laptop or a spare computer you can keep **offline** (Wi-Fi off, no cable).
- A USB stick with a copy of this repo (download it on your normal computer).
- Paper and pen, and ideally a metal backup plate or stamp kit.
- 30 minutes alone.

## Steps

1. **Go offline.** On the spare computer, turn off Wi-Fi/Bluetooth and unplug the network
   cable. Keep it offline for the whole ceremony.
2. **Copy the repo** from the USB stick to the computer and open a terminal in that folder.
3. **Generate the keypair** (Python 3 with `cryptography` installed, as for a node):
   `python3 ops/pause_keygen.py`. Type `YES` to confirm you are offline. It prints a
   **PRIVATE KEY** and a **PUBLIC KEY** to the screen only. It writes no file.
4. **Write the private key down** on paper twice, carefully. Read it back
   character by character. Stamp or engrave it on the metal plate if you have one.
5. **Write down the public key** separately, labelled "PAUSE PUBLIC KEY".
6. **Test once, still offline.** Run `python3 ops/pause_tx.py pause "test" --nonce 1`,
   type the private key at the hidden prompt, and check that the printed `"pubkey"` equals
   your public key. Do not broadcast it.
7. **Wipe:** delete any file holding the private key, empty the trash, and clear the
   terminal history (`history -c`). Better still, never save it to a file.
8. **Store the backups** in two separate safe places (e.g. a home safe and a bank box).
   Never photograph them. Never type them into a phone, chat, email or cloud.
9. **Hand over only the public key:** paste the PUBLIC key into the PR or chat as
   `PAUSE_PUBKEY = "..."`. It is safe to share.

## Using it later

Pausing needs the offline computer and the paper key. Sign the pause, renew, or
resume tx offline with `ops/pause_tx.py`, carry the signed tx over on USB, and
broadcast it. A pause ends by itself after 10,080 blocks (about 7 days) unless you renew it.

If the private key is ever lost or exposed, set a new `PAUSE_PUBKEY` in a new release.
