import argparse
import logging
import os
import sys

DEFAULT_PORT = 8000


def add_node_args(parser):
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--data-dir",
        default=".",
        help="folder for chain.json, mempool.json, peers.json and "
        "state_contributions.json (created if missing). Default: current folder.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument(
        "--peer", action="append", default=[], metavar="URL",
        help="peer to connect to, e.g. http://127.0.0.1:8001 (repeatable)",
    )
    parser.add_argument(
        "--advertise-url", default=None, metavar="URL",
        help="URL other machines should use to reach this node (needed across machines)",
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run a Netlithic node.")
    add_node_args(parser)
    return parser.parse_args(argv)


def use_data_dir(path: str) -> str:
    """Create the data dir if needed and make it the working directory, so all
    of the node's json files live there. Must run before importing p2p_http."""
    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    os.chdir(path)
    return path


def start(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    data_dir = use_data_dir(args.data_dir)

    print("Starting Netlithic Node...")
    print(f"Data dir: {data_dir}")

    from p2p_http import run_node

    run_node(port=args.port, host=args.host, advertise_url=args.advertise_url, extra_peers=args.peer)


if __name__ == "__main__":
    start(parse_args())