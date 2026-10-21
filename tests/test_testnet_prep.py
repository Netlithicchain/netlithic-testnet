"""Testnet prep: chain_id from config, no private keys printed, transaction fees."""
import copy
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

import core
import economics as eco
import wallet as wallet_mod
from conftest import ROOT, build_block, post
from core import BLOCK_REWARD, Blockchain, make_reward_tx

NLT = eco.BASE_UNITS_PER_NLT
MIN_FEE = 10_000  # 0.0001 NLT


def fund(bc, w, blocks=1):
    for _ in range(blocks):
        assert bc.add_block(bc.mine_block(w.address, []).to_dict())


def write_cfg(tmp_path, **changes):
    cfg = json.load(open(eco.CONFIG_PATH))
    cfg.update(changes)
    path = tmp_path / "genesis_config.json"
    path.write_text(json.dumps(cfg))
    return str(path)


# --- 1) chain_id comes from the config ----------------------------------------------

def test_repo_config_is_the_chain_id_source():
    cfg = json.load(open(eco.CONFIG_PATH))
    assert wallet_mod.current_chain_id() == cfg["chain_id"] == eco.load_economics().chain_id
    assert cfg["fees"]["min_fee_base_units"] == MIN_FEE == eco.load_economics().min_fee


def test_wallet_signs_with_configured_chain_id(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.setenv(wallet_mod.GENESIS_CONFIG_ENV, write_cfg(tmp_path, chain_id="netlithic-testnet-1"))
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    assert tx["payload"]["chain_id"] == "netlithic-testnet-1"
    assert wallet_mod.verify_signed_tx(tx)                                  # follows config
    assert not wallet_mod.verify_signed_tx(tx, chain_id="netlithic-dev-1")  # other network


def test_node_rejects_tx_signed_for_another_chain(chain, wallets, tmp_path, monkeypatch):
    alice, bob = wallets
    fund(chain, alice)
    foreign = alice.create_transaction(bob.address, NLT, nonce=1, chain_id="netlithic-testnet-1")
    assert not chain.validate_candidate_tx_against_chain_and_mempool(foreign, [])
    block = build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD, MIN_FEE), foreign])
    assert not chain.add_block(block.to_dict())
    # a different chain_id means a different genesis: our chain.json is refused there
    with pytest.raises(RuntimeError):
        Blockchain(econ=replace(chain.econ, chain_id="netlithic-testnet-1"))
    # ...and on a fresh node whose config says netlithic-testnet-1 the same tx is valid
    (tmp_path / "other").mkdir(); monkeypatch.chdir(tmp_path / "other")
    other = Blockchain(econ=replace(chain.econ, chain_id="netlithic-testnet-1"))
    fund(other, alice)
    assert other.validate_candidate_tx_against_chain_and_mempool(foreign, [])


def test_missing_chain_id_in_config_is_an_error(tmp_path):
    cfg = json.load(open(eco.CONFIG_PATH)); cfg.pop("chain_id")
    p = tmp_path / "c.json"; p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="chain_id"):
        eco.load_economics(str(p))


def test_dev_features_still_gated_to_dev_ids(tmp_path):
    p = write_cfg(tmp_path, chain_id="netlithic-testnet-1")
    cfg = json.load(open(p)); cfg["treasury_spend_approval"]["mode"] = "single_key_dev"
    open(p, "w").write(json.dumps(cfg))
    with pytest.raises(ValueError):
        eco.load_economics(p)


def test_status_and_chain_report_chain_id(node):
    import urllib.request
    for path in ("/status", "/chain"):
        with urllib.request.urlopen(node.self_url() + path, timeout=5) as r:
            assert json.loads(r.read())["chain_id"] == "netlithic-dev-1"


@pytest.mark.parametrize("path,body", [
    ("/hello", {"url": "http://127.0.0.1:9"}),
    ("/receive_tx", {"tx": {}}),
    ("/receive_block", {"block": {}}),
    ("/submit_tx", {"tx": {}}),
])
def test_peer_messages_from_another_chain_are_refused(node, path, body):
    status, resp = post(node, path, dict(body, chain_id="netlithic-other-9"))
    assert status == 400 and "chain_id" in resp["error"]


def test_hello_refuses_peer_whose_status_is_another_chain(node, node_procs, tmp_path, monkeypatch):
    monkeypatch.setenv("NETLITHIC_GENESIS_CONFIG", write_cfg(tmp_path, chain_id="netlithic-dev-other"))
    b = node_procs()  # a real node running the other chain_id
    status, resp = post(node, "/hello", {"url": f"http://127.0.0.1:{b}"})  # no chain_id in body
    assert status == 400 and "different chain_id" in resp["error"]
    assert not node.peers


# --- 2) no private keys printed ------------------------------------------------------

def test_wallet_script_prints_only_address_and_file_is_0600(tmp_path):
    out = subprocess.run([sys.executable, os.path.join(ROOT, "wallet.py"), "carol"], cwd=tmp_path,
                         capture_output=True, text=True, check=True)
    data = json.load(open(tmp_path / "wallets.json"))
    (addr, w), = data.items()
    assert addr in out.stdout
    assert w["private_key"] not in out.stdout + out.stderr
    assert "private_key" not in out.stdout
    assert os.stat(tmp_path / "wallets.json").st_mode & 0o777 == 0o600


def test_no_script_prints_private_keys():
    for name in ("wallet.py", "send_tx.py", "new_genesis.py", "netlithic.py", "node.py",
                 "p2p_http.py", "wallet_test.py"):
        src = open(os.path.join(ROOT, name)).read()
        for line in src.splitlines():
            if "print(" in line or "_print(" in line:
                assert "private_key" not in line, f"{name}: {line.strip()}"


# --- 3) fees ---------------------------------------------------------------------------

def test_sender_pays_amount_plus_fee_and_miner_collects_it(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, 4 * NLT, nonce=1, fee=3 * MIN_FEE)
    block = chain.mine_block(bob.address, [tx])
    assert block.transactions[0]["fees"] == 3 * MIN_FEE
    assert chain.add_block(block.to_dict())
    assert chain.get_balance(alice.address) == BLOCK_REWARD - 4 * NLT - 3 * MIN_FEE
    assert chain.get_balance(bob.address) == BLOCK_REWARD + 4 * NLT + 3 * MIN_FEE


def test_fees_do_not_mint(chain, wallets):
    alice, bob = wallets
    fund(chain, alice, 2)
    for n in (1, 2):
        tx = alice.create_transaction(bob.address, NLT, nonce=n, fee=5 * MIN_FEE)
        assert chain.add_block(chain.mine_block(bob.address, [tx]).to_dict())
    assert sum(chain.get_balances().values()) == chain.total_minted()
    assert chain.rewards_paid() == 4 * BLOCK_REWARD  # fees aren't counted as minted
    assert chain.total_minted() <= eco.MAX_SUPPLY


def test_fee_below_minimum_or_missing_is_rejected(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    low = alice.create_transaction(bob.address, NLT, nonce=1, fee=MIN_FEE - 1)
    assert not chain.validate_candidate_tx_against_chain_and_mempool(low, [])
    assert not chain.add_block(build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD, MIN_FEE - 1), low]).to_dict())
    nofee = copy.deepcopy(low); del nofee["payload"]["fee"]
    with pytest.raises(ValueError):
        core.normalize_signed_tx(nofee)


def test_fee_is_signed(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    tx["payload"]["fee"] = 10 * MIN_FEE  # tamper
    assert not chain.validate_candidate_tx_against_chain_and_mempool(tx, [])


def test_balance_must_cover_amount_plus_fee(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    assert not chain.validate_candidate_tx_against_chain_and_mempool(
        alice.create_transaction(bob.address, BLOCK_REWARD, nonce=1), [])
    assert chain.validate_candidate_tx_against_chain_and_mempool(
        alice.create_transaction(bob.address, BLOCK_REWARD - MIN_FEE, nonce=1), [])


@pytest.mark.parametrize("claimed", [0, MIN_FEE - 1, MIN_FEE + 1])
def test_miner_must_claim_exact_fees(chain, wallets, claimed):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    block = build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD, claimed), tx])
    assert not chain.add_block(block.to_dict())


def test_miner_picks_highest_fee_first(chain, wallets, monkeypatch):
    alice, bob = wallets
    fund(chain, alice); fund(chain, bob)
    cheap = alice.create_transaction(bob.address, NLT, nonce=1, fee=MIN_FEE)
    rich = bob.create_transaction(alice.address, NLT, nonce=1, fee=50 * MIN_FEE)
    monkeypatch.setattr(core, "MAX_BLOCK_TXS", 1)
    block = chain.mine_block(alice.address, [cheap, rich])
    assert [t["payload"]["sender"] for t in block.transactions[1:]] == [bob.address]
    assert chain.add_block(block.to_dict())
    too_many = build_block(chain, [make_reward_tx(bob.address, chain.expected_reward(chain.chain), 2 * MIN_FEE),
                                    cheap, alice.create_transaction(bob.address, NLT, nonce=2)])
    assert not chain.add_block(too_many.to_dict())  # over MAX_BLOCK_TXS


def test_cli_send_with_custom_fee(capsys, node, monkeypatch):
    import netlithic as cli
    cli.main(["wallet", "new", "alice"]); cli.main(["wallet", "new", "bob"])
    cli.main(["mine", "alice", "--node", node.self_url()])
    capsys.readouterr()
    assert cli.main(["send", "alice", "bob", "1", "--fee", "0.001", "--node", node.self_url()]) == 0
    assert "fee 0.001 NLT" in capsys.readouterr().out
