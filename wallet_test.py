import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request
from netutil import urlopen

from wallet import Wallet

NODE_URL = "http://127.0.0.1:8000"


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str) -> dict:
    req = Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )

    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_title(text: str) -> None:
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


def print_json(data: dict) -> None:
    print(json.dumps(data, indent=2))


def wait_for_node(seconds: int = 1) -> None:
    time.sleep(seconds)


def get_balance(address: str) -> dict:
    return get_json(f"{NODE_URL}/balance?address={address}")


def get_account(address: str) -> dict:
    return get_json(f"{NODE_URL}/account?address={address}")


def main() -> None:
    try:
        print_title("STEP 1 - CREATE WALLETS")
        miner = Wallet.create("Miner")
        receiver = Wallet.create("Receiver")

        print(f"Miner name:       {miner.name}")
        print(f"Miner address:    {miner.address}")
        print(f"Receiver name:    {receiver.name}")
        print(f"Receiver address: {receiver.address}")

        print_title("STEP 2 - CHECK STARTING BALANCES")
        print("Miner balance:")
        print_json(get_balance(miner.address))
        print("Receiver balance:")
        print_json(get_balance(receiver.address))

        print_title("STEP 3 - MINE A BLOCK TO REWARD THE MINER")
        mine_result_1 = post_json(
            f"{NODE_URL}/mine",
            {"miner_address": miner.address}
        )
        print_json(mine_result_1)

        wait_for_node(1)

        print_title("STEP 4 - CHECK BALANCES AFTER MINING")
        print("Miner balance:")
        print_json(get_balance(miner.address))
        print("Receiver balance:")
        print_json(get_balance(receiver.address))

        print_title("STEP 5 - SEND TRANSACTION FROM MINER TO RECEIVER")
        account_state = get_account(miner.address)
        tx = miner.create_transaction(
            to_address=receiver.address,
            amount=5,
            nonce=account_state["next_nonce"],
        )

        tx_result = post_json(
            f"{NODE_URL}/submit_tx",
            {"tx": tx}
        )
        print_json(tx_result)

        wait_for_node(1)

        print_title("STEP 6 - MINE AGAIN TO CONFIRM THE TRANSACTION")
        mine_result_2 = post_json(
            f"{NODE_URL}/mine",
            {"miner_address": miner.address}
        )
        print_json(mine_result_2)

        wait_for_node(1)

        print_title("STEP 7 - CHECK FINAL BALANCES")
        print("Miner balance:")
        print_json(get_balance(miner.address))
        print("Receiver balance:")
        print_json(get_balance(receiver.address))

        print_title("STEP 8 - VIEW FULL CHAIN")
        chain = get_json(f"{NODE_URL}/chain")
        print_json(chain)

    except HTTPError as e:
        print("\nHTTP ERROR")
        print(f"Status: {e.code}")
        try:
            print(e.read().decode("utf-8"))
        except Exception:
            pass

    except URLError as e:
        print("\nURL ERROR")
        print("Could not connect to the node.")
        print("Make sure your node is running on http://127.0.0.1:8000")
        print(f"Details: {e}")

    except Exception as e:
        print("\nERROR")
        print(str(e))


if __name__ == "__main__":
    main()