import os, sys
from collections import namedtuple
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))
import healthcheck as H

Disk = namedtuple("Disk", "total used free")


def cfg(**kw):
    c = H.load_config(None)
    c.update(kw)
    return c


def fetcher(heights, faucet=1000):
    def fetch(url):
        if url.endswith("/api/info"):
            return {"balance_nlt": str(faucet)}
        for node, h in heights.items():
            if url.startswith(node):
                if h is None:
                    raise OSError("down")
                return {"height": h}
        raise OSError("unknown")
    return fetch


def run(c, state, now, heights, faucet=1000, days=60, free=50):
    return H.run_checks(c, state, now, fetch=fetcher(heights, faucet), cert_fn=lambda h: days,
                        disk_fn=lambda p: Disk(100, 100 - free, free))


A, B = "http://127.0.0.1:8000", "http://127.0.0.1:8001"


def test_disabled_by_default():
    assert H.DEFAULTS["enabled"] is False


def test_healthy():
    p, _ = run(cfg(), {}, 0, {A: 10, B: 10})
    assert p == {}


def test_stall_out_of_sync_down_faucet_cert_disk():
    c = cfg()
    _, st = run(c, {}, 0, {A: 10, B: 10})
    p, st = run(c, st, 31 * 60, {A: 10, B: 2})
    assert "stalled:" + A in p and "out_of_sync" in p
    p, _ = run(c, st, 0, {A: None, B: 10}, faucet=5, days=3, free=5)
    assert {"down:" + A, "faucet_low", "cert:testnet.netlithicchain.com", "disk:/"} <= set(p)


def test_alerts_dedupe_and_recover():
    c = cfg()
    msgs, st = H.plan_alerts({"faucet_low": "low"}, {}, c, 0)
    assert msgs == ["low"]
    msgs, st = H.plan_alerts({"faucet_low": "low"}, st, c, 60)
    assert msgs == []
    msgs, st = H.plan_alerts({"faucet_low": "low"}, st, c, c["repeat_minutes"] * 60 + 1)
    assert msgs == ["low"]
    msgs, st = H.plan_alerts({}, st, c, 10 ** 6)
    assert msgs and msgs[0].startswith("Recovered")


def test_deliver_sends_nothing_when_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr(H, "send_webhook", lambda u, t: sent.append(t))
    H.deliver(cfg(webhook_url="http://hook"), ["x"])
    assert sent == []
    H.deliver(cfg(webhook_url="http://hook", enabled=True), ["x"])
    assert len(sent) == 1
