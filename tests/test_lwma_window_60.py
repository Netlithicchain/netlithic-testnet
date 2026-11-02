"""The difficulty average covers the last 60 blocks and the target block time stays 60 seconds."""
import core
from core import Block

T = core.LWMA_TARGET_BLOCK_TIME


def fake(index, ts, work):
    return Block(index=index, timestamp=ts, transactions=[], previous_hash="", nonce=0, difficulty=work, hash="")


def test_window_and_target_constants():
    assert core.LWMA_WINDOW == 60
    assert core.LWMA_TARGET_BLOCK_TIME == 60
    assert core.STALL_SECONDS == 10 * core.LWMA_TARGET_BLOCK_TIME


def test_only_the_last_60_blocks_count():
    """Blocks older than the window have no influence on the next work; the 60th-last block still does."""
    def chain_with(old_work):
        ch = [fake(4999, 1.79e9, 2)]
        ch += [fake(5000 + i, 1.79e9 + 60 * (i + 1), old_work if i < 40 else 5000) for i in range(100)]
        return ch
    assert core.lwma_next_work(chain_with(10_000)) == core.lwma_next_work(chain_with(1_000_000))   # both outside the window
    ch = [fake(4999, 1.79e9, 2)] + [fake(5000 + i, 1.79e9 + 60 * (i + 1), 5000) for i in range(100)]
    changed = list(ch)
    changed[-60] = fake(changed[-60].index, changed[-60].timestamp, 7000)                  # inside the window
    assert core.lwma_next_work(changed) != core.lwma_next_work(ch)
    outside = list(ch)
    outside[-61] = fake(outside[-61].index, outside[-61].timestamp, 7000)                   # just outside
    assert core.lwma_next_work(outside) == core.lwma_next_work(ch)


def test_steady_blocks_at_target_keep_the_work():
    ch = [fake(4999, 1.79e9, 2)] + [fake(5000 + i, 1.79e9 + T * (i + 1), 123456) for i in range(120)]
    assert core.lwma_next_work(ch) == 123456
