import json, os, threading, urllib.request, urllib.error
import pytest

from conftest import TEST_ADMIN_TOKEN

import faucet as faucet_mod
from faucet import Faucet, FaucetError, RateLimiter, load_faucet_wallet

A1 = "a" * 32
A2 = "b" * 32


class FakeNode:
    def __init__(self, chain_id="netlithic-dev-1", balance=10**12):
        self.cid, self.balance, self.submitted, self.code = chain_id, balance, [], 200

    def status(self):
        return {"chain_id": self.cid}

    def account(self, address):
        return {"balance": self.balance, "next_nonce": len(self.submitted) + 1}

    def submit(self, tx):
        self.submitted.append(tx)
        return self.code, {"tx_id": "t%d" % len(self.submitted)}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def fw(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from wallet import Wallet
    return Wallet.create("faucet")


def make(fw, node=None, cap=10**12, clock=None, path=None):
    lim = RateLimiter(path, cooldown=24 * 3600, daily_cap=cap, clock=clock or Clock())
    return Faucet(fw, node or FakeNode(), lim, amount=10 * 10**8)


def test_drip_signs_fixed_amount(fw):
    node = FakeNode()
    r = make(fw, node).drip(A1, "1.2.3.4")
    assert r["ok"] and r["amount_nlt"] == "10"
    tx = node.submitted[0]
    assert tx["payload"]["recipient"] == A1 and tx["payload"]["amount"] == 10 * 10**8
    from wallet import verify_signed_tx
    assert verify_signed_tx(tx)


@pytest.mark.parametrize("bad", ["", "xyz", "g" * 32, "a" * 31, "a" * 33, None, "<script>"])
def test_rejects_invalid_address(fw, bad):
    with pytest.raises(FaucetError) as ex:
        make(fw).drip(bad, "1.1.1.1")
    assert ex.value.status == 400


def test_rejects_own_address(fw):
    with pytest.raises(FaucetError):
        make(fw).drip(fw.address, "1.1.1.1")


def test_per_address_and_ip_limits_and_expiry(fw):
    clock = Clock()
    f = make(fw, clock=clock)
    f.drip(A1, "1.1.1.1")
    with pytest.raises(FaucetError) as ex:
        f.drip(A1, "9.9.9.9")
    assert ex.value.status == 429 and "address" in str(ex.value)
    with pytest.raises(FaucetError) as ex:
        f.drip(A2, "1.1.1.1")
    assert "IP" in str(ex.value)
    clock.t += 24 * 3600 + 1
    f.drip(A1, "1.1.1.1")


def test_daily_cap(fw):
    f = make(fw, cap=25 * 10**8)
    f.drip(A1, "1.1.1.1")
    f.drip(A2, "2.2.2.2")
    with pytest.raises(FaucetError) as ex:
        f.drip("c" * 32, "3.3.3.3")
    assert "daily" in str(ex.value)


def test_limits_persist(fw, tmp_path):
    p = str(tmp_path / "state.json")
    clock = Clock()
    make(fw, clock=clock, path=p).drip(A1, "1.1.1.1")
    with pytest.raises(FaucetError):
        make(fw, clock=clock, path=p).drip(A1, "5.5.5.5")


def test_refuses_non_dev_chain(fw):
    node = FakeNode(chain_id="netlithic-main-1")
    with pytest.raises(FaucetError) as ex:
        make(fw, node).drip(A1, "1.1.1.1")
    assert ex.value.status == 503 and not node.submitted


def test_empty_faucet_and_node_reject_do_not_burn_limit(fw):
    node = FakeNode(balance=0)
    f = make(fw, node)
    with pytest.raises(FaucetError):
        f.drip(A1, "1.1.1.1")
    node.balance, node.code = 10**12, 400
    with pytest.raises(FaucetError):
        f.drip(A1, "1.1.1.1")
    node.code = 200
    assert f.drip(A1, "1.1.1.1")["ok"]


def test_key_file_must_be_0600_and_never_leaks(fw, tmp_path, capsys):
    path = str(tmp_path / "wallets.json")
    assert load_faucet_wallet(path).address == fw.address
    if os.name != "nt":
        os.chmod(path, 0o644)
        with pytest.raises(FaucetError) as ex:
            load_faucet_wallet(path)
        assert fw.private_key not in str(ex.value)
        os.chmod(path, 0o600)
    with pytest.raises(FaucetError):
        load_faucet_wallet(None)
    with pytest.raises(FaucetError):
        load_faucet_wallet(str(tmp_path / "missing.json"))


def test_main_refuses_loose_key_file(fw, tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "wallets.json")
    os.chmod(path, 0o640)
    monkeypatch.setenv(faucet_mod.KEY_FILE_ENV, path)
    assert faucet_mod.main(["--node", "http://127.0.0.1:9"]) == 1
    out = capsys.readouterr()
    assert fw.private_key not in out.out + out.err


def _serve(f):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), faucet_mod.make_handler(f))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_form_and_api_escape(fw):
    srv, url = _serve(make(fw))
    try:
        page = urllib.request.urlopen(url + "/").read().decode()
        assert "test coins have no value" in page and "<form" in page
        code, body = _post(url + "/", b"address=%3Cscript%3Ealert(1)%3C%2Fscript%3E", "application/x-www-form-urlencoded")
        assert code == 400 and "<script>alert" not in body and "&lt;script&gt;" in body
        code, body = _post(url + "/api/drip", json.dumps({"address": A1}).encode(), "application/json")
        assert code == 200 and json.loads(body)["ok"]
        code, body = _post(url + "/api/drip", json.dumps({"address": A1}).encode(), "application/json")
        assert code == 429
        code, _ = _post(url + "/api/drip", b"x" * 5000, "application/json")
        assert code == 413
    finally:
        srv.shutdown()


def test_end_to_end_against_real_node(node, tmp_path):
    """Fund the faucet by mining to it on a local dev node, then drip."""
    from wallet import Wallet
    fwallet = Wallet.create("faucet")
    base = f"http://127.0.0.1:{node.PORT}"
    req = urllib.request.Request(base + "/mine", data=json.dumps({"miner_address": fwallet.address}).encode(),
                                 headers={"Content-Type": "application/json", "X-Admin-Token": TEST_ADMIN_TOKEN})
    urllib.request.urlopen(req, timeout=120).read()
    f = Faucet(fwallet, faucet_mod.NodeClient(base), RateLimiter(None, daily_cap=10**12), amount=10**8)
    r = f.drip(A1, "127.0.0.1")
    assert r["ok"] and r["tx_id"]
    assert len(node.mempool) == 1 and node.mempool[0]["payload"]["recipient"] == A1
