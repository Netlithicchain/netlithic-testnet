import importlib, json, os, socket, subprocess, sys, threading, time
import urllib.error, urllib.request
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """Fresh Blockchain in a throwaway dir, so chain.json / wallets.json /
    state_contributions.json are written to tmp_path, never the repo."""
    monkeypatch.chdir(tmp_path)
    from core import Blockchain
    return Blockchain()


@pytest.fixture
def wallets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from wallet import Wallet
    return Wallet.create("alice"), Wallet.create("bob")


def build_block(bc, txs, chain=None, difficulty=None, timestamp=None):
    """Mine a block with an arbitrary transaction list on top of `chain`."""
    from core import Block, meets_pow
    chain = chain if chain is not None else bc.chain
    prev = chain[-1]
    if difficulty is None:
        difficulty = bc.expected_block_difficulty(chain)
    ts = timestamp if timestamp is not None else max(time.time(), prev.timestamp + 1)
    nonce = 0
    while True:
        h = bc.calculate_hash(index=prev.index + 1, timestamp=ts, transactions=txs,
                              previous_hash=prev.hash, nonce=nonce, difficulty=difficulty)
        if meets_pow(h, difficulty, prev.index + 1):
            return Block(index=prev.index + 1, timestamp=ts, transactions=txs,
                         previous_hash=prev.hash, nonce=nonce, difficulty=difficulty, hash=h)
        nonce += 1


TEST_ADMIN_TOKEN = "test-admin-token-not-secret"


@pytest.fixture
def node(tmp_path, monkeypatch):
    """An in-process node on a random localhost port, data in tmp_path.
    No sync thread is started, so it never contacts any peer."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETLITHIC_ADMIN_TOKEN", TEST_ADMIN_TOKEN)
    # Local test nets run on 127.0.0.1; production refuses private peers (R1/R2).
    monkeypatch.setenv("NETLITHIC_ALLOW_PRIVATE_PEERS", "1")
    monkeypatch.delenv("NETLITHIC_DEV_LOCAL_ADMIN", raising=False)
    import p2p_http
    p2p = importlib.reload(p2p_http)  # fresh Blockchain() in tmp_path
    server = p2p.ThreadingHTTPServer(("127.0.0.1", 0), p2p.P2PHandler)
    p2p.HOST, p2p.PORT = "127.0.0.1", server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield p2p
    server.shutdown()
    server.server_close()


def post(p2p, path, payload=None, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"{p2p.self_url()}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Admin-Token": TEST_ADMIN_TOKEN, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")




def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


def wait_status(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as r:
                return json.loads(r.read())
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"node on {port} did not start")


@pytest.fixture
def node_procs(tmp_path):
    """Start real node.py processes on free localhost ports; killed afterwards."""
    procs = []

    def start(*extra):
        port = free_port()
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "node.py"), str(port),
                              "--data-dir", str(tmp_path / f"node{port}"), *extra],
                             env={**os.environ, "NETLITHIC_ADMIN_TOKEN": TEST_ADMIN_TOKEN,
                                  "NETLITHIC_ALLOW_PRIVATE_PEERS": "1"},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        wait_status(port)
        return port

    yield start
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


@pytest.fixture(autouse=True)
def _fresh_checkpoint_cache():
    """Checkpoints are cached per process (R5); give each test a clean cache."""
    import core
    core.reset_checkpoints()
    yield
    core.reset_checkpoints()


@pytest.fixture(autouse=True)
def _registry_off_unless_asked(monkeypatch, request):
    """The shipped build has the registered-contributors option ON (and, until the key is set, fail-safe: nobody earns
    transfer points). The older points tests describe the open transfer pot (points v4), which is exactly what the
    registry-disabled mode is, so they run with it off. Tests that need the registry say `@pytest.mark.registry`
    (and use the helpers in tests/registry_helpers.py)."""
    if "registry" in request.keywords:
        return
    import registry
    monkeypatch.setattr(registry, "REGISTRY_ENABLED", False)


def pytest_configure(config):
    config.addinivalue_line("markers", "registry: run with the registered-contributors option as shipped (on)")
    config.addinivalue_line("markers", "real_valid_cap: keep the production cap on registry valid_until (no test-only lift)")


@pytest.fixture(autouse=True)
def _registry_valid_until_cap(request, monkeypatch):
    """Older registry tests sign with a far-future valid_until (registry.NO_EXPIRY). Consensus now caps valid_until at
    REGISTRY_MAX_VALID_BLOCKS past the carrying block, so those tests run with the cap lifted. Tests that exercise the
    real cap are marked @pytest.mark.real_valid_cap and run with the production value."""
    if request.node.get_closest_marker("real_valid_cap"):
        return
    if "node" in request.fixturenames or "node_procs" in request.fixturenames:
        return      # the cap is part of the rules fingerprint: an in-process node must match the subprocess nodes
    try:
        import registry
    except Exception:  # pragma: no cover
        return
    monkeypatch.setattr(registry, "REGISTRY_MAX_VALID_BLOCKS", 10 ** 13)


def read_private_doc(*parts):
    """Read a doc that lives only in the private netlithic-core repo (runbooks, points v4 notes).
    In the public repo those files are not published, so the test that checks their wording is skipped."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, *parts)
    if not os.path.exists(path):
        pytest.skip("doc is kept in the private repo, not published here: " + "/".join(parts))
    with open(path, encoding="utf-8") as fh:
        return fh.read()
