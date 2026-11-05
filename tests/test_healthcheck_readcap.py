import os, sys, json, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ops"))
import healthcheck as H


class FakeResp:
    def __init__(self, body, cl="auto"):
        self.body = body
        self.headers = {} if cl is None else {"Content-Length": str(len(body) if cl == "auto" else cl)}
    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch(monkeypatch, resp):
    monkeypatch.setattr(H, "_safe_urlopen", lambda *a, **k: resp)


def test_over_1mib_parses(monkeypatch):
    body = json.dumps({"chain": ["x" * 100] * 20000}).encode()
    assert len(body) > (1 << 20)
    _patch(monkeypatch, FakeResp(body))
    assert len(H.http_json("http://x/chain")["chain"]) == 20000


def test_over_limit_clear_error(monkeypatch):
    _patch(monkeypatch, FakeResp(b'{"a":"' + b"x" * 200 + b'"}', cl=None))
    with pytest.raises(H.ReplyTooLarge, match="larger than limit"):
        H.http_json("http://x/chain", limit=100)


def test_mismatched_length_clear_error(monkeypatch):
    _patch(monkeypatch, FakeResp(b'{"a":1}', cl=500))
    with pytest.raises(H.TruncatedReply, match="truncated reply"):
        H.http_json("http://x/chain")


def test_default_limit_is_32mib():
    assert H.MAX_REPLY_BYTES == 32 * 1024 * 1024
