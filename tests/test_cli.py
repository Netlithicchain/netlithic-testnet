import json
import os

import pytest

import netlithic as cli


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_wallet_new_list_show_never_prints_private_key(capsys):
    code, out, _ = run(capsys, "wallet", "new", "alice")
    assert code == 0 and "Address:" in out
    priv = next(iter(json.load(open("wallets.json")).values()))["private_key"]
    assert priv not in out

    code, out, _ = run(capsys, "wallet", "list")
    assert code == 0 and out.startswith("alice: ") and priv not in out

    code, out, err = run(capsys, "wallet", "show", "alice")
    assert code == 0 and priv not in out and priv not in err
    assert json.loads(out)["name"] == "alice"


def test_wallet_export_key_writes_0600_file_never_prints(capsys):
    run(capsys, "wallet", "new", "alice")
    priv = next(iter(json.load(open("wallets.json")).values()))["private_key"]
    code, out, err = run(capsys, "wallet", "show", "alice", "--export-key", "alice.key")
    assert code == 0 and priv not in out and priv not in err
    assert json.load(open("alice.key"))["private_key"] == priv
    assert os.stat("alice.key").st_mode & 0o777 == 0o600
    code, _, err = run(capsys, "wallet", "show", "alice", "--export-key", "alice.key")
    assert code == 1 and "already exists" in err  # never overwrites a key file
    with pytest.raises(SystemExit):
        cli.main(["wallet", "show", "alice", "--show-private"])  # old flag is gone


def test_wallet_show_unknown(capsys):
    code, _, err = run(capsys, "wallet", "show", "nobody")
    assert code == 1 and "No wallet" in err


def test_custom_wallet_file(capsys, tmp_path):
    run(capsys, "wallet", "new", "bob", "--wallets", "other.json")
    assert (tmp_path / "other.json").exists() and not (tmp_path / "wallets.json").exists()


def test_default_port_is_8000():
    a = cli.build_parser().parse_args(["node", "start", "--data-dir", "d", "--peer", "http://127.0.0.1:8001"])
    assert a.port == 8000 and a.peer == ["http://127.0.0.1:8001"] and a.data_dir == "d"
    assert cli.build_parser().parse_args(["status"]).node == "http://127.0.0.1:8000"


def test_genesis_requires_yes_then_validate(capsys, tmp_path):
    code, _, err = run(capsys, "genesis", "--data-dir", "d")
    assert code == 1 and "--yes" in err
    code, out, _ = run(capsys, "genesis", "--data-dir", str(tmp_path / "d"), "--yes")
    assert code == 0 and (tmp_path / "d" / "chain.json").exists()

    code, out, _ = run(capsys, "validate", "--data-dir", str(tmp_path / "d"))
    assert code == 0 and "Valid chain. Height 0" in out


def test_validate_detects_tampering(capsys, tmp_path):
    run(capsys, "genesis", "--data-dir", str(tmp_path / "d"), "--yes")
    p = tmp_path / "d" / "chain.json"
    chain = json.loads(p.read_text()); chain[0]["nonce"] += 1; p.write_text(json.dumps(chain))
    code, out, _ = run(capsys, "validate", "--data-dir", str(tmp_path / "d"))
    assert code == 1 and "INVALID" in out
    assert json.loads(p.read_text())[0]["nonce"] == chain[0]["nonce"]  # not overwritten


def test_status_mine_send_balance_against_node(capsys, node):
    url = node.self_url()
    run(capsys, "wallet", "new", "alice"); run(capsys, "wallet", "new", "bob")
    bob = [w for w in json.load(open("wallets.json")).values() if w["name"] == "bob"][0]["address"]

    code, out, _ = run(capsys, "mine", "alice", "--node", url)
    assert code == 0 and "Mined block 1" in out

    code, out, _ = run(capsys, "send", "alice", "bob", "4", "--node", url)
    assert code == 0 and "Transaction accepted" in out

    code, out, _ = run(capsys, "mine", "bob", "--node", url)
    assert code == 0 and "(1 txs" in out

    code, out, _ = run(capsys, "balance", bob, "--node", url)
    assert json.loads(out)["balance_nlt"] == "14.0001"  # 10 reward + 4 + 0.0001 fee collected

    code, out, _ = run(capsys, "status", "--node", url)
    assert json.loads(out)["height"] == 2


def test_send_overspend_shows_node_error(capsys, node):
    run(capsys, "wallet", "new", "alice"); run(capsys, "wallet", "new", "bob")
    code, _, err = run(capsys, "send", "alice", "bob", "5", "--node", node.self_url())
    assert code == 1 and "HTTP 400" in err


def test_unreachable_node_is_a_clean_error(capsys):
    code, _, err = run(capsys, "status", "--node", "http://127.0.0.1:9")
    assert code == 1 and "Could not reach node" in err


def test_mine_sends_admin_token(capsys, node, monkeypatch):
    from conftest import TEST_ADMIN_TOKEN
    url = node.self_url()
    run(capsys, "wallet", "new", "carol")
    # --admin-token overrides the env var; a wrong one is refused
    code, _, err = run(capsys, "mine", "carol", "--node", url, "--admin-token", "wrong")
    assert code != 0 and "403" in err
    # env var NETLITHIC_ADMIN_TOKEN (set by the fixture) is used by default
    code, out, _ = run(capsys, "mine", "carol", "--node", url)
    assert code == 0 and "Mined block 1" in out
    code, out, _ = run(capsys, "mine", "carol", "--node", url, "--admin-token", TEST_ADMIN_TOKEN)
    assert code == 0 and "Mined block 2" in out
