"""Timestamp validation hardening, part 3: transaction timestamps must be finite (single-signer and multisig
paths), and the node's JSON answers never contain the non-standard number tokens (not-a-number, infinity)."""
import io
import json
import math
import urllib.request

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import core
import wallet as wallet_mod
from wallet import Wallet

WIRE_VALUES = [math.inf - math.inf, math.inf, -math.inf, "nan", "inf", "-inf", "1e999"]
WIRE_IDS = [f"wire{i}" for i in range(len(WIRE_VALUES))]
BLOCK_VALUES = ["nan", math.inf]
BLOCK_IDS = ["wire0", "wire1"]
JSON_VALUES = [math.inf - math.inf, math.inf, -math.inf]
JSON_IDS = ["value0", "value1", "value2"]


def _sign_transfer(w, ts):
    """Sign a transfer by hand (the wallet helper stamps the current time itself)."""
    payload = {"sender": w.address, "recipient": "cd" * 16, "amount": 5, "nonce": 1,
               "timestamp": ts, "fee": 1, "chain_id": wallet_mod.current_chain_id()}
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(w.private_key))
    sig = sk.sign(wallet_mod.signing_message(payload, w.pubkey)).hex()
    return {"payload": payload, "pubkey": w.pubkey, "sig": sig}


def _multisig(w, ts):
    payload = {"bucket": "treasury", "recipient": "cd" * 16, "amount": 5, "nonce": 1,
               "timestamp": ts, "chain_id": wallet_mod.current_chain_id()}
    return {"type": "multisig_spend", "payload": payload, "sigs": [w.sign_multisig(payload)]}


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # wallet file goes to the throwaway dir
    return Wallet.create("tester")


# ---- single-signer transfer -------------------------------------------------------------

@pytest.mark.parametrize("wire", WIRE_VALUES, ids=WIRE_IDS)
def test_non_finite_tx_timestamp_is_refused_by_normalize_tx(w, wire):
    with pytest.raises(ValueError):
        core.normalize_tx(_sign_transfer(w, wire))


@pytest.mark.parametrize("wire", WIRE_VALUES, ids=WIRE_IDS)
def test_non_finite_tx_timestamp_is_refused_by_verify_signed_tx_even_when_signed(w, wire):
    tx = _sign_transfer(w, wire)
    assert wallet_mod.verify_signed_tx(tx) is False


def test_non_finite_tx_timestamp_a_finite_signed_tx_still_verifies_and_normalizes(w):
    tx = _sign_transfer(w, 1.79e9)
    assert wallet_mod.verify_signed_tx(tx) is True
    assert core.normalize_tx(tx)["payload"]["timestamp"] == 1.79e9


@pytest.mark.parametrize("wire", BLOCK_VALUES, ids=BLOCK_IDS)
def test_non_finite_tx_timestamp_keeps_a_block_with_it_out(chain, w, wire):
    """A block carrying such a transfer is refused (its transactions cannot even be read)."""
    from conftest import build_block
    reward = core.make_reward_tx(w.address, chain.expected_reward(chain.chain))
    blk = build_block(chain, [reward]).to_dict()
    blk["transactions"].append(_sign_transfer(w, wire))
    assert chain.add_block(blk) is False
    assert len(chain.chain) == 1


# ---- multisig path ------------------------------------------------------------------------

@pytest.mark.parametrize("wire", WIRE_VALUES, ids=WIRE_IDS)
def test_non_finite_tx_timestamp_is_refused_on_the_multisig_path(w, wire):
    with pytest.raises(ValueError):
        core.normalize_tx(_multisig(w, wire))
    with pytest.raises(ValueError):
        core.normalize_multisig_tx(_multisig(w, wire))


def test_non_finite_tx_timestamp_multisig_finite_and_missing_still_normalize(w):
    assert core.normalize_tx(_multisig(w, 1.79e9))["payload"]["timestamp"] == 1.79e9
    tx = _multisig(w, 1.0)
    del tx["payload"]["timestamp"]
    assert core.normalize_tx(tx)["payload"]["timestamp"] == 0.0     # unchanged behaviour: a missing time is 0


# ---- JSON output ---------------------------------------------------------------------------

class _FakeHandler:
    path = "/test"

    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass


@pytest.mark.parametrize("bad", JSON_VALUES, ids=JSON_IDS)
def test_json_response_never_emits_non_standard_tokens(node, bad):
    h = _FakeHandler()
    node._json_response(h, 200, {"ok": True, "t": bad, "nested": [1, {"x": bad}]})
    body = h.wfile.getvalue()
    assert h.status == 500                       # a safe fixed error, no crash
    assert b"NaN" not in body and b"Infinity" not in body
    assert "error" in json.loads(body)           # valid standard JSON
    assert h.headers["Content-Length"] == str(len(body))


def test_json_response_normal_payload_is_unchanged(node):
    h = _FakeHandler()
    node._json_response(h, 201, {"a": 1, "t": 1.5})
    assert h.status == 201
    assert json.loads(h.wfile.getvalue()) == {"a": 1, "t": 1.5}


def test_json_response_over_http_status_is_standard_json(node):
    with urllib.request.urlopen(f"{node.self_url()}/status", timeout=10) as r:
        raw = r.read().decode()
    assert "NaN" not in raw and "Infinity" not in raw
    json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(c)))
