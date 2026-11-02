"""Desktop app stays on the public network: tip hash and height are compared with the
seeds, the app says so when it is not on the network and stops mining, it pulls and
adopts only a longer VALID chain, and the seeds never fetch from a sender they cannot trust."""
import json
import os
import sys
import time
from urllib.request import urlopen

import pytest

from conftest import build_block, post

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "desktop"))
import netlithic_desktop as D  # noqa: E402
import core  # noqa: E402
from core import BLOCK_REWARD, make_reward_tx  # noqa: E402

ADDR = "ab" * 16
SEED = "http://seed"
LOCAL = "http://local"


class Net:
    """Scripted local node and one seed. `mine` / `blocks` / `received` record what the app asked for."""

    def __init__(self, local, seed, local_blocks=None, seed_status=None):
        self.local, self.seed = local, seed          # (height, hash)
        self.blocks = local_blocks or {}             # index -> block dict (our own node's chain)
        self.calls, self.received = [], []
        self.seed_extra = seed_status or {}

    def __call__(self, url, payload=None, headers=None):
        self.calls.append(url)
        if url == LOCAL + "/status":
            return 200, {"height": self.local[0], "latest_hash": self.local[1], "checkpoints_ok": True}
        if url == SEED + "/status":
            return 200, {"height": self.seed[0], "latest_hash": self.seed[1], "chain_id": "t", **self.seed_extra}
        if url.startswith(LOCAL + "/block?index="):
            i = int(url.rsplit("=", 1)[1])
            blk = self.blocks.get(i)
            if blk is None and i == self.local[0]:  # our tip, made just now
                blk = {"index": i, "hash": self.local[1], "timestamp": time.time() - 60}
            return (200, {"block": blk}) if blk else (404, {})
        if url == SEED + "/receive_block":
            self.received.append(payload["block"])
            return 200, {"message": "Block accepted"}
        if url == LOCAL + "/mine":
            return 200, {}
        if url == LOCAL + "/sync":
            return 200, {}
        return 404, {}

    def count(self, suffix):
        return len([u for u in self.calls if u.endswith(suffix)])


def app(net):
    return D.Earner(LOCAL, SEED, net, admin_token="t" * 10, pace=0.01)


# ---- the app against the seeds ---------------------------------------------------------------

def test_forked_ahead_app_is_not_on_the_network_and_does_not_mine():
    # we are at 12 but our block 10 is not the seeds' block 10: a private fork
    net = Net(local=(12, "mine12"), seed=(10, "seed10"), local_blocks={10: {"index": 10, "hash": "mine10"}})
    e = app(net)
    s = e.status()
    assert s["network_state"] == "forked" and not s["synced"] and not s["on_network"]
    assert "Not on the network" in s["sync"] and "Connected and up to date" not in s["sync"]
    with pytest.raises(ValueError):
        e.start(ADDR)
    e.address = ADDR
    wait = e.step()
    assert net.count("/mine") == 0 and wait <= D.RECHECK_SECONDS
    assert net.count("/sync") == 1          # it asks its own node to pull instead
    assert "Not on the network" in e.last_error


def test_same_height_competing_block_pauses_mining():
    net = Net(local=(10, "mine10"), seed=(10, "seed10"))
    e = app(net)
    e.address = ADDR
    s = e.status()
    assert s["network_state"] == "forked" and not s["synced"] and "Not on the network" in s["sync"]
    e.step()
    assert net.count("/mine") == 0
    # once the node adopts the network's block, mining resumes
    net.local = (10, "seed10")
    assert e.status()["synced"] and "up to date" in e.status()["sync"]
    e.step()
    assert net.count("/mine") == 1


def test_one_block_behind_does_not_mine():
    net = Net(local=(9, "seed9"), seed=(10, "seed10"))
    e = app(net)
    e.address = ADDR
    assert e.status()["network_state"] == "behind"
    e.step()
    assert net.count("/mine") == 0 and net.count("/sync") == 1


def test_ahead_on_the_seeds_chain_waits_and_hands_the_seed_its_next_block():
    blocks = {10: {"index": 10, "hash": "seed10"}, 11: {"index": 11, "hash": "mine11", "previous_hash": "seed10"}}
    net = Net(local=(11, "mine11"), seed=(10, "seed10"), local_blocks=blocks)
    e = app(net)
    e.address = ADDR
    s = e.status()
    assert s["network_state"] == "ahead_waiting" and not s["synced"]
    assert "Waiting for the network" in s["sync"]
    e.step()
    assert net.count("/mine") == 0
    assert [b["hash"] for b in net.received] == ["mine11"]   # pushed to the seed, never to anyone else
    assert all(u.startswith((LOCAL, SEED)) for u in net.calls)


def test_relay_never_pushes_to_a_seed_on_another_chain():
    blocks = {10: {"index": 10, "hash": "mine10"}, 11: {"index": 11, "hash": "mine11"}}
    net = Net(local=(11, "mine11"), seed=(10, "seed10"), local_blocks=blocks)
    assert app(net).relay([(SEED, {"height": 10, "latest_hash": "seed10"})]) == 0
    assert net.received == []


def test_seeds_that_disagree_pause_mining():
    answers = {"http://a": (10, "x"), "http://b": (10, "y")}

    def call(url, payload=None, headers=None):
        if url == LOCAL + "/status":
            return 200, {"height": 10, "latest_hash": "x"}
        for k, (h, th) in answers.items():
            if url == k + "/status":
                return 200, {"height": h, "latest_hash": th}
        return 404, {}
    e = D.Earner(LOCAL, ["http://a", "http://b"], call, admin_token="t" * 10, pace=0.01)
    s = e.status()
    assert s["network_state"] == "seeds_disagree" and not s["synced"]


def test_bad_checkpoint_state_is_not_on_the_network():
    net = Net(local=(10, "seed10"), seed=(10, "seed10"))
    orig = net.__call__

    def call(url, payload=None, headers=None):
        code, d = orig(url, payload, headers)
        if url == LOCAL + "/status":
            d["checkpoints_ok"] = False
        return code, d
    assert app(call).status()["network_state"] == "forked"


def test_running_loop_never_mines_while_forked():
    net = Net(local=(10, "mine10"), seed=(10, "seed10"))
    e = app(net)
    e.running, e.address = True, ADDR
    import threading
    th = threading.Thread(target=e._loop, daemon=True)
    th.start()
    time.sleep(0.3)
    e.stop()
    th.join(2)
    assert net.count("/mine") == 0 and net.count("/sync") >= 1


# ---- the node adopts a longer VALID chain, and only that -------------------------------------

def _grow(bc, base, n, miner):
    out = list(base)
    for _ in range(n):
        out.append(build_block(bc, [make_reward_tx(miner, BLOCK_REWARD)], chain=out))
    return out


def test_longer_valid_public_chain_replaces_local_only_blocks(chain, wallets, tmp_path):
    alice, bob = wallets
    wfile = tmp_path / "wallets.json"
    before = wfile.read_bytes() if wfile.exists() else None
    for _ in range(3):                                  # blocks only this computer has
        assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    public = _grow(chain, chain.chain[:1], 5, bob.address)
    assert chain.replace_chain([b.to_dict() for b in public])
    assert chain.get_latest_block().hash == public[-1].hash and len(chain.chain) == 6
    assert chain.get_balance(alice.address) == 0 and chain.get_balance(bob.address) == 5 * BLOCK_REWARD
    after = wfile.read_bytes() if wfile.exists() else None
    assert after == before                              # the wallet file was not touched


def test_invalid_longer_chain_is_rejected(chain, wallets):
    alice, bob = wallets
    for _ in range(2):
        assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    tip = chain.get_latest_block().hash
    public = _grow(chain, chain.chain[:1], 5, bob.address)
    bad = [b.to_dict() for b in public]
    bad[3]["transactions"][0]["amount"] = BLOCK_REWARD * 1000      # a forged reward
    assert not chain.replace_chain(bad)
    broken = [b.to_dict() for b in public]
    broken[2]["previous_hash"] = "0" * 64                          # does not link
    assert not chain.replace_chain(broken)
    assert chain.get_latest_block().hash == tip and len(chain.chain) == 3


def test_longer_chain_that_breaks_a_checkpoint_is_rejected(chain, wallets, monkeypatch, tmp_path):
    alice, bob = wallets
    for _ in range(2):
        assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    tip = chain.get_latest_block().hash
    public = _grow(chain, chain.chain[:1], 5, bob.address)
    cp = tmp_path / "cp.json"
    cp.write_text(json.dumps({"2": "0" * 64}))
    monkeypatch.setattr(core, "CHECKPOINTS_FILE", str(cp))
    core.reset_checkpoints()
    try:
        assert not chain.replace_chain([b.to_dict() for b in public])
    finally:
        core.reset_checkpoints()
    assert chain.get_latest_block().hash == tip


def test_equal_work_competing_block_is_not_adopted(chain, wallets):
    alice, bob = wallets
    assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    rival = _grow(chain, chain.chain[:1], 1, bob.address)
    assert not chain.replace_chain([b.to_dict() for b in rival])   # a tie never flips the node


# ---- the seed side -----------------------------------------------------------------------------

def _far_block(node):
    """A block that is several heights ahead of the node's tip (it can never be added directly)."""
    tip = node.blockchain.get_latest_block()
    return {"index": tip.index + 5, "previous_hash": "ab" * 32, "hash": "cd" * 32, "timestamp": time.time(),
            "transactions": [], "nonce": 0, "difficulty": 1}


def test_seed_refuses_to_fetch_from_a_spoofed_loopback_sender(node, monkeypatch):
    monkeypatch.setattr(node, "ALLOW_PRIVATE_PEERS", False)      # how a public node runs
    calls = []
    monkeypatch.setattr(node, "_sync_with_peer", lambda peer: calls.append(peer))
    for spoof in ("http://127.0.0.1:47101", "http://localhost:47101", "http://10.0.0.5:47101", "http://[::1]:47101"):
        code, res = post(node, "/receive_block", {"block": _far_block(node), "from_peer": spoof,
                                                   "chain_id": node.chain_id()})
        assert code == 200 and "no sync triggered" in res["message"], (spoof, res)
    # even a loopback address that IS in the peer list is refused (it would be the seed fetching from itself)
    with node.state_lock:
        node.peers.add("http://127.0.0.1:47101")
    code, res = post(node, "/receive_block", {"block": _far_block(node), "from_peer": "http://127.0.0.1:47101",
                                               "chain_id": node.chain_id()})
    assert "no sync triggered" in res["message"]
    assert calls == []


def test_seed_still_syncs_from_a_known_public_peer(node, monkeypatch):
    monkeypatch.setattr(node, "ALLOW_PRIVATE_PEERS", False)
    monkeypatch.setattr(node, "peer_address_allowed", lambda url, resolver=None: True)
    calls = []
    monkeypatch.setattr(node, "_sync_with_peer", lambda peer: calls.append(peer))
    with node.state_lock:
        node.peers.add("http://203.0.113.9:47101")
    code, res = post(node, "/receive_block", {"block": _far_block(node), "from_peer": "http://203.0.113.9:47101",
                                               "chain_id": node.chain_id()})
    assert code == 200 and "sync attempted" in res["message"] and calls == ["http://203.0.113.9:47101"]


def test_loopback_node_does_not_name_itself_as_a_sender(node, monkeypatch):
    monkeypatch.setattr(node, "ALLOW_PRIVATE_PEERS", False)
    monkeypatch.setattr(node, "ADVERTISE_URL", "")
    assert node._advertised_sender() is None
    monkeypatch.setattr(node, "HOST", "203.0.113.9")
    assert node._advertised_sender() == "http://203.0.113.9:%s" % node.PORT
    monkeypatch.setattr(node, "HOST", "127.0.0.1")
    monkeypatch.setattr(node, "ADVERTISE_URL", "https://seed.example")
    assert node._advertised_sender() == "https://seed.example"


def test_block_endpoint_returns_one_block(node):
    with urlopen(f"{node.self_url()}/block?index=0", timeout=5) as r:
        blk = json.loads(r.read())["block"]
    assert blk["index"] == 0 and blk["hash"] == node.blockchain.chain[0].hash
    import urllib.error
    for q, want in (("index=99999", 404), ("index=-1", 400), ("index=x", 400), ("", 400)):
        with pytest.raises(urllib.error.HTTPError) as ei:
            urlopen(f"{node.self_url()}/block?{q}", timeout=5)
        assert ei.value.code == want


def test_app_sees_a_real_node_ahead_of_the_seed(node):
    """Our own node mines two blocks; the 'seed' is one block behind on the same chain, then on another."""
    for _ in range(2):
        code, _ = post(node, "/mine", {"miner_address": ADDR})
        assert code == 200
    chain = node.blockchain.chain
    h_seed = chain[-2]

    def call(url, payload=None, headers=None):
        if url.startswith(node.self_url()):
            from urllib.error import HTTPError
            import urllib.request
            try:
                with urlopen(urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                                    headers={"Content-Type": "application/json"}), timeout=5) as r:
                    return r.status, json.loads(r.read())
            except HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        return 200, {"height": h_seed.index, "latest_hash": call.seed_hash}
    call.seed_hash = h_seed.hash
    e = D.Earner(node.self_url(), SEED, call, admin_token="t" * 10, pace=0.01)
    assert e.position()[0] == "ahead_waiting"
    call.seed_hash = "ee" * 32
    assert e.position()[0] == "forked"


def _get_block_status(node, query):
    import urllib.error
    try:
        with urlopen(f"{node.self_url()}/block?{query}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.mark.parametrize("query", [
    "index=1_0",                 # int() accepts an underscore between digits
    "index=%2B1", "index=%2D1",  # signs (+1, -1)
    "index=%201", "index=1%20",  # spaces around the number
    "index=1%0A",                # trailing newline
    "index=%D9%A0",              # an Arabic-Indic digit
    "index=%EF%BC%90",           # a full-width digit
    "index=0x1", "index=1e2", "index=1.0", "index=",
    "index=1234567890123",       # more digits than any real height
    "idx=0",
])
def test_block_endpoint_rejects_anything_but_ascii_digits(node, query):
    status, body = _get_block_status(node, query)
    assert status == 400 and "error" in body


@pytest.mark.parametrize("query,want", [("index=0", 200), ("index=00", 200), ("index=000000", 200),
                                        ("index=7", 404), ("index=999999999999", 404)])
def test_block_endpoint_accepts_plain_digits(node, query, want):
    status, body = _get_block_status(node, query)
    assert status == want
    if want == 200:
        assert body["block"]["index"] == 0


def test_block_endpoint_uses_only_the_first_index_value(node):
    assert _get_block_status(node, "index=0&index=x")[0] == 200
    assert _get_block_status(node, "index=x&index=0")[0] == 400


# ---- release anchors and seed coverage (display only) ----
def _anchored(local_hash, seed_hash, local_h=20, seed_h=20, seed_block_ok=True, local_block_ok=True,
              tip_time=None, now=None):
    anchor = {5: "a" * 64}
    tip = {"index": local_h, "hash": "tip", "timestamp": time.time() - 60 if tip_time is None else tip_time}

    def call(url, payload=None, headers=None):
        if url == LOCAL + "/status":
            return 200, {"height": local_h, "latest_hash": "tip"}
        if url == SEED + "/status":
            return 200, {"height": seed_h, "latest_hash": "tip"}
        if url == LOCAL + f"/block?index={local_h}":
            return 200, {"block": tip}
        if url == LOCAL + "/block?index=5":
            return (200, {"block": {"index": 5, "hash": local_hash}}) if local_block_ok else (500, {})
        if url == SEED + "/block?index=5":
            return (200, {"block": {"index": 5, "hash": seed_hash}}) if seed_block_ok else (404, {})
        return 404, {}
    clock = (lambda: now) if now is not None else None
    return D.Earner(LOCAL, SEED, call, admin_token="t" * 10, pace=0.01, anchors=anchor, clock=clock)


def test_matching_release_anchor_reports_up_to_date():
    assert _anchored("a" * 64, "a" * 64).status()["synced"] is True


def test_eclipsed_seeds_off_release_anchor_are_not_up_to_date():
    s = _anchored("b" * 64, "b" * 64).status()
    assert s["network_state"] == "forked" and not s["synced"] and not s["on_network"]


def test_seed_off_release_anchor_is_forked_even_while_behind():
    s = _anchored("a" * 64, "b" * 64, local_h=3, seed_h=20).status()
    assert s["network_state"] == "forked"


def test_seed_anchor_block_unreadable_is_not_up_to_date():
    s = _anchored("a" * 64, None, seed_block_ok=False).status()
    assert s["network_state"] == "checking" and not s["synced"] and not s["can_help"]
    assert _anchored("b" * 64, None, seed_block_ok=False).status()["network_state"] == "forked"


def test_local_anchor_block_unreadable_is_not_up_to_date():
    s = _anchored(None, "a" * 64, local_block_ok=False).status()
    assert s["network_state"] == "checking" and not s["synced"]


def test_checking_state_does_not_mine():
    e = _anchored("a" * 64, None, seed_block_ok=False)
    mined = []
    e.mine_once = lambda: mined.append(1)
    e.step()
    assert mined == []


def test_old_network_tip_is_not_shown_up_to_date_but_can_help():
    s = _anchored("a" * 64, "a" * 64, tip_time=1000.0, now=1000.0 + D.STALE_TIP_SECONDS + 60).status()
    assert s["network_state"] == "stale" and not s["synced"] and s["can_help"]
    assert "up to date" not in s["sync"].replace("not shown as up to date", "")


def test_recent_network_tip_is_up_to_date():
    s = _anchored("a" * 64, "a" * 64, tip_time=1000.0, now=1000.0 + 60).status()
    assert s["network_state"] == "ok" and s["synced"]


def test_old_network_tip_still_mines():
    e = _anchored("a" * 64, "a" * 64, tip_time=1000.0, now=1000.0 + D.STALE_TIP_SECONDS + 60)
    mined = []
    e.mine_once = lambda: mined.append(1)
    e.step()
    assert mined == [1]


def test_shipped_anchor_is_wellformed():
    for h, v in D.DESKTOP_ANCHORS.items():
        assert isinstance(h, int) and h > 0 and len(v) == 64 and int(v, 16) >= 0


def test_up_to_date_says_when_only_some_servers_answered():
    def call(url, payload=None, headers=None):
        if url == LOCAL + "/status":
            return 200, {"height": 10, "latest_hash": "x"}
        if url == "http://a/status":
            return 200, {"height": 10, "latest_hash": "x"}
        if url == LOCAL + "/block?index=10":
            return 200, {"block": {"index": 10, "hash": "x", "timestamp": time.time() - 60}}
        return None, {}
    e = D.Earner(LOCAL, ["http://a", "http://b"], call, admin_token="t" * 10, pace=0.01)
    s = e.status()
    assert s["synced"] and "1 of 2" in s["sync"]


def test_tip_time_comes_from_our_own_tip_block_not_the_seed():
    # a seed claiming a fresh tip time does not count; our own tip block is old
    e = _anchored("a" * 64, "a" * 64, tip_time=1000.0, now=1000.0 + D.STALE_TIP_SECONDS + 60)
    real = e.call

    def call(url, payload=None, headers=None):
        code, data = real(url, payload, headers)
        if url == SEED + "/status":
            data = dict(data, latest_timestamp=1000.0 + D.STALE_TIP_SECONDS + 50)
        return code, data
    e.call = call
    assert e.status()["network_state"] == "stale"


@pytest.mark.parametrize("bad_time", [None, "1700000000", True, float("inf"), float("-inf"), float("nan"), [1], {}],
                         ids=["missing", "text", "bool", "big", "small", "undefined", "list", "dict"])
def test_untrusted_tip_time_is_checking_not_up_to_date(bad_time):
    e = _anchored("a" * 64, "a" * 64, now=10_000.0)
    real = e.call

    def call(url, payload=None, headers=None):
        code, data = real(url, payload, headers)
        if url == LOCAL + "/block?index=20":
            blk = dict(data["block"])
            if bad_time is None:
                blk.pop("timestamp")
            else:
                blk["timestamp"] = bad_time
            data = {"block": blk}
        return code, data
    e.call = call
    s = e.status()
    assert s["network_state"] == "checking" and not s["synced"] and not s["can_help"]


def test_tip_block_unreadable_is_checking():
    e = _anchored("a" * 64, "a" * 64, now=10_000.0)
    real = e.call
    e.call = lambda url, payload=None, headers=None: (500, {}) if url == LOCAL + "/block?index=20" \
        else real(url, payload, headers)
    assert e.status()["network_state"] == "checking"


def test_tip_block_with_another_hash_is_checking():
    e = _anchored("a" * 64, "a" * 64, now=10_000.0)
    real = e.call

    def call(url, payload=None, headers=None):
        code, data = real(url, payload, headers)
        if url == LOCAL + "/block?index=20":
            data = {"block": dict(data["block"], hash="other")}
        return code, data
    e.call = call
    assert e.status()["network_state"] == "checking"


def test_tip_time_far_in_the_future_is_checking():
    s = _anchored("a" * 64, "a" * 64, tip_time=10_000.0 + D.MAX_TIP_AHEAD_SECONDS + 60, now=10_000.0).status()
    assert s["network_state"] == "checking" and not s["synced"]


def test_tip_time_slightly_ahead_is_up_to_date():
    s = _anchored("a" * 64, "a" * 64, tip_time=10_000.0 + 600, now=10_000.0).status()
    assert s["network_state"] == "ok" and s["synced"]


def test_tip_time_as_integer_is_accepted():
    s = _anchored("a" * 64, "a" * 64, tip_time=9_940, now=10_000.0).status()
    assert s["network_state"] == "ok"
