import json
import urllib.request

import pytest

from conftest import build_block
from core import BLOCK_REWARD, make_reward_tx


def cp_file():
    with open("state_contributions.json") as f:
        return json.load(f)


def test_points_computed_from_chain(chain, wallets):
    alice, bob = wallets
    assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    tx = alice.create_transaction(bob.address, 10**8, nonce=1)
    assert chain.add_block(chain.mine_block(bob.address, [tx]).to_dict())
    pts = chain.contribution_points()
    assert pts[alice.address] == {"cp_tenths": 10, "cp": "1", "blocks_mined": 1, "empty_blocks": 1, "tx_count": 1, "point_tx_count": 1}
    assert pts[bob.address] == {"cp_tenths": 25, "cp": "2.5", "blocks_mined": 1, "empty_blocks": 0, "tx_count": 0, "point_tx_count": 0}
    assert cp_file()["users"] == pts and cp_file()["height"] == 2


def test_genesis_earns_nothing(chain):
    assert chain.contribution_points() == {}
    assert cp_file()["users"] == {}


def test_tampered_cache_is_rebuilt_on_load(chain, wallets):
    alice, _ = wallets
    assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    with open("state_contributions.json", "w") as f:
        json.dump({"users": {alice.address: {"cp": 9999}}}, f)
    from core import Blockchain
    Blockchain()
    assert cp_file()["users"][alice.address]["cp"] == "0"
    assert cp_file()["users"][alice.address]["blocks_mined"] == 1


def test_deleted_cache_comes_back(chain, wallets, tmp_path):
    alice, _ = wallets
    assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    (tmp_path / "state_contributions.json").unlink()
    from core import Blockchain
    Blockchain()
    assert cp_file()["users"][alice.address]["blocks_mined"] == 1


def test_reorg_moves_points_to_winning_chain(chain, wallets):
    alice, bob = wallets
    assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    longer = list(chain.chain[:1])
    for _ in range(2):
        longer.append(build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD)], chain=longer))
    assert chain.replace_chain([b.to_dict() for b in longer])
    assert alice.address not in cp_file()["users"]
    assert cp_file()["users"][bob.address]["blocks_mined"] == 2


def test_contributions_endpoint_and_status(node, wallets):
    alice, bob = wallets
    bc = node.blockchain
    assert bc.add_block(bc.mine_block(alice.address, []).to_dict())
    tx = alice.create_transaction(bob.address, 10**8, nonce=1)
    assert bc.add_block(bc.mine_block(bob.address, [tx]).to_dict())
    with urllib.request.urlopen(f"{node.self_url()}/contributions") as r:
        data = json.loads(r.read())
    assert data["height"] == 2
    assert [(c["address"], c["cp"]) for c in data["contributors"]] == [(bob.address, "2.5"), (alice.address, "1")]
    assert data["rules"]["per_block_mined"] == "2.5" and data["rules"]["per_tx_sent"] == "1"
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        assert json.loads(r.read())["contributors"] == 2


def test_cli_points(node, wallets, capsys):
    import netlithic as cli
    alice, _ = wallets
    bc = node.blockchain
    assert bc.add_block(bc.mine_block(alice.address, []).to_dict())
    assert cli.main(["points", "--node", node.self_url()]) == 0
    out = capsys.readouterr().out
    assert alice.address in out and "+2.5 per block mined" in out and "1 empty" in out
