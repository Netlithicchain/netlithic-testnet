"""Strict JSON parsing on everything a node receives."""
import io

import pytest

from conftest import post


class _Handler:
    def __init__(self, raw: bytes):
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)


@pytest.mark.parametrize("token", ["Infinity", "-Infinity", "NaN"])
def test_request_body_number_constants_refused(node, token):
    with pytest.raises(ValueError):
        node._read_json(_Handler(('{"x": %s}' % token).encode()))


def test_plain_request_body_still_parses(node):
    assert node._read_json(_Handler(b'{"x": 1.5, "y": [1, 2]}')) == {"x": 1.5, "y": [1, 2]}


@pytest.mark.parametrize("fn", ["_get_json", "_post_json"])
def test_peer_responses_refuse_number_constants(node, fn, monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(node, "urlopen", lambda *a, **k: _Resp(b'{"height": Infinity}'))
    args = ("http://peer.example/x",) + (({},) if fn == "_post_json" else ())
    with pytest.raises(ValueError):
        getattr(node, fn)(*args)


def test_receive_block_with_number_constant_is_invalid_json(node):
    code, body = post(node, "/receive_block", raw=b'{"block": {"index": 1, "nonce": Infinity}}')
    assert code == 400 and body.get("error") == "Invalid JSON"


# ---- shared strict loader (node, web wallet, desktop, tools) ----
import importlib.util
import os

import strict_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD_DOCS = ['{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}', '{"x": 1e999}', '{"x": -1e999}']


def _ops(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "ops", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("doc", BAD_DOCS)
def test_shared_loader_refuses_out_of_range_numbers(doc):
    with pytest.raises(ValueError):
        strict_json.loads(doc)


def test_shared_loader_keeps_plain_json():
    assert strict_json.loads('{"a": 1, "b": 2.5, "c": 1e300, "d": [true, null, "x"]}') == \
        {"a": 1, "b": 2.5, "c": 1e300, "d": [True, None, "x"]}


@pytest.mark.parametrize("doc", BAD_DOCS)
def test_node_parser_refuses_out_of_range_numbers(node, doc):
    with pytest.raises(ValueError):
        node._read_json(_Handler(doc.encode()))


@pytest.mark.parametrize("tool", ["healthcheck", "founder_miner"])
@pytest.mark.parametrize("doc", BAD_DOCS)
def test_standalone_tools_parse_strictly(tool, doc):
    loads = _ops(tool)._strict_loads
    with pytest.raises(ValueError):
        loads(doc)
    assert loads('{"h": 3, "t": 1.5}') == {"h": 3, "t": 1.5}


def test_web_wallet_upstream_parses_strictly(monkeypatch):
    import webwallet

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(webwallet, "urlopen", lambda *a, **k: _Resp(b'{"height": 1e999}'))
    up = webwallet.Upstream.__new__(webwallet.Upstream)
    up.timeout = 1
    with pytest.raises(ValueError):
        up.call("http://seed.example/status")


def test_receive_block_with_oversized_number_is_invalid_json(node):
    code, body = post(node, "/receive_block", raw=b'{"block": {"index": 1, "nonce": 1e999}}')
    assert code == 400 and body.get("error") == "Invalid JSON"


def _saved_chain(chain):
    import core
    from conftest import build_block
    reward = core.make_reward_tx("ab" * 16, chain.expected_reward(chain.chain))
    assert chain.add_block(build_block(chain, [reward]).to_dict()) is True
    chain.save_chain()
    with open(core.CHAIN_FILE, encoding="utf-8") as f:
        return f.read()


def test_saved_chain_file_still_loads(chain):
    import core
    _saved_chain(chain)
    again = core.Blockchain()
    assert len(again.chain) == 2 and again.chain[-1].hash == chain.chain[-1].hash


def test_chain_file_with_out_of_range_number_is_not_loaded(chain):
    import core
    text = _saved_chain(chain)
    with open(core.CHAIN_FILE, "w", encoding="utf-8") as f:
        f.write(text.replace('"index": 0,', '"x": 1e999, "index": 0,', 1))
    with pytest.raises(RuntimeError, match="not a valid chain"):  # refused, and never silently overwritten
        core.Blockchain()


def test_family_issuer_check_reads_files_strictly(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "check_family_issuers", os.path.join(os.path.dirname(__file__), "..", "ops", "check_family_issuers.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text('{"chain": [{"index": 0, "timestamp": 1.5}]}')
    bad.write_text('{"chain": [{"index": 0, "timestamp": 1e999}]}')
    assert mod.load(str(good)) == [{"index": 0, "timestamp": 1.5}]
    with pytest.raises(ValueError):
        mod.load(str(bad))
