#!/usr/bin/env bash
# Snapshot node data (chain, mempool, peers, contributions cache) before a deploy.
# Run as root on the server. Never copies wallet or key files.
# Usage: ops/backup_chain.sh /var/lib/netlithic/nodeA /var/lib/netlithic/nodeB
set -euo pipefail
STAMP=$(date +%Y%m%d-%H%M%S)
DEST=/var/backups/netlithic/$STAMP
mkdir -p "$DEST"
for dir in "$@"; do
  name=$(basename "$dir")
  mkdir -p "$DEST/$name"
  for f in chain.json mempool.json peers.json state_contributions.json; do
    [ -f "$dir/$f" ] && cp -p "$dir/$f" "$DEST/$name/$f"
  done
done
chmod -R go-rwx "$DEST"
echo "$DEST"
