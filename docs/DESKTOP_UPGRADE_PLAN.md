# Desktop app: upgrade before block ~4900 (review F3)

Desktop builds made from the old `desktop-release` branch use the old stall rule and have no
pause, 720-block reorg limit or anti-farming v2. **They split off the network at block 5000.**

## What 0.5.0 changes
- The desktop build bundles the same node code as this release. `tests/test_review_fixes.py`
  checks that every module the node imports is a `--hidden-import` in all three build scripts.
  This caught `history`, which was missing (the address-history page would have broken in the app).
  `config/` (genesis + checkpoints.json) is shipped too.
- **Version gate.** `/status` reports `version`, `min_peer_version` (0.5.0) and
  `upgrade_deadline_height` (4900). Builds before 0.5.0 report no version, so they count as outdated.
  - Below block 4900, `/hello` still accepts an outdated peer but answers `upgrade_needed: true` with a warning.
  - From block 4900, `/hello` refuses outdated peers with "Upgrade needed".
- The 0.5.0+ desktop app reads the seed's `min_peer_version`. If the app is older than that,
  it shows "Update needed: please install Netlithic X or newer before block N" on the status line.
  This only helps for *future* upgrades: old builds can't learn new tricks, so they have to be reached through announcements.

## Timeline (60 s blocks after 5000; ~10 min today)
| When | Action |
|---|---|
| Release (~Nov 9) | Publish 0.5.0 desktop builds for Win/macOS/Linux plus checksums, after Aric approves. The founder server goes first. |
| Release +1 day | Marketing sends the required-update notice (after Aric approves). |
| Weekly | Check the explorer's "nodes ready" count, then remind anyone who hasn't updated. |
| Block ~4800 | Final reminder. |
| Block 4900 | Upgraded nodes refuse new handshakes from pre-0.5.0 nodes. |
| Block 5000 | Fork. Anyone still on an old build is on a dead branch: install 0.5.0 and resync (data and wallets are kept). |

Installers are **not** built by this change. Builds and publishing need Aric's yes.
