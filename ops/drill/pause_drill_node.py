"""Node launcher for the LOCAL pause-switch drill. Box/test use only.

Starts a normal node process but first scales the consensus constants down so
a drill finishes in seconds instead of days. It also sets a THROWAWAY pause
public key given by the drill harness. Never run this on a public node: it
deliberately changes consensus constants, so it only makes sense on a private
chain whose every node uses it.

env (all optional):
  DRILL_PAUSE_PUBKEY   hex public key allowed to pause (the drill's throwaway key)
  DRILL_SEC_FORK       height of the security fork (default 3); a very large
                       number simulates an OLD node that does not know the fork
  DRILL_EPOCH_LEN      payout period length in blocks (default 4)
  DRILL_EXPIRY         pause expiry in blocks (default 6)
usage: python ops/drill/pause_drill_node.py PORT --data-dir DIR [--peer URL ...]
"""
import os
import sys
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import core                      # noqa: E402
import economics as eco          # noqa: E402
import pause                     # noqa: E402
import contributions             # noqa: E402

fork = int(os.environ.get("DRILL_SEC_FORK", "3"))
epoch = int(os.environ.get("DRILL_EPOCH_LEN", "4"))
core.SECURITY_FORK_HEIGHT = fork
core.DIFFICULTY_ADJUSTMENT_INTERVAL = 10 ** 9          # constant difficulty, fast blocks
contributions.MIN_ADDRESS_AGE_BLOCKS = 0
pause.PAUSE_EXPIRY_BLOCKS = int(os.environ.get("DRILL_EXPIRY", "6"))
pause.PAUSE_PUBKEY = os.environ.get("DRILL_PAUSE_PUBKEY", "")
eco.TESTNET_SHORT_EPOCHS["netlithic-dev-1"] = (min(fork, 3), epoch)

_orig_load = eco.load_economics


def _scaled(*a, **kw):
    e = _orig_load(*a, **kw)
    return replace(e, blocks_per_month=10, epoch_blocks=100, vesting_epochs=2,
                   unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})


eco.load_economics = _scaled

import node  # noqa: E402

if __name__ == "__main__":
    node.start(node.parse_args())
