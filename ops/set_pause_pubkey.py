"""Put the pause PUBLIC key into pause.py (the only place it goes), or check that it is there.

    python ops/set_pause_pubkey.py <64-hex PUBLIC key>     set the slot (refuses if already set; --force replaces)
    python ops/set_pause_pubkey.py --check                 release gate: exit 0 only if the slot holds a valid key
    python ops/set_pause_pubkey.py --status                show the slot (empty / key fingerprint), always exit 0

Safety rules built in:
  * Only a PUBLIC key is wanted: 64 hex characters that are a valid Ed25519 point. A private key is also 64 hex,
    so this tool cannot tell them apart. Use the line labelled "PUBLIC KEY" from ops/pause_keygen.py, and check
    that ops/pause_tx.py prints the same "pubkey" (docs/PAUSE_KEY_CEREMONY.md step 6).
  * It writes only pause.py (or --file PATH), changes only the one PAUSE_PUBKEY line, and shows the key in groups of
    8 characters plus its short key id so you can compare it with the founder's paper copy character by character.
  * It never generates a key and never reads a key file.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SLOT_RE = re.compile(r'^PAUSE_PUBKEY = "([^"\n]*)"(  # PAUSE-PUBKEY-SLOT.*)$', re.M)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def validate(key: str) -> str:
    """Return the normalised (lowercase) key or raise ValueError with a plain-English reason."""
    key = key.strip().lower()
    if key.startswith("0x"):
        key = key[2:]
    if not HEX64.match(key):
        raise ValueError("A pause public key is exactly 64 hex characters (0-9, a-f). Got "
                         f"{len(key)} characters or non-hex characters.")
    if set(key) == {"0"}:
        raise ValueError("All-zero key is not a real key.")
    if not _is_good_point(bytes.fromhex(key)):
        raise ValueError("That is not a valid Ed25519 public key (it is not a point on the curve, or it is a "
                         "weak low-order point). Re-copy the PUBLIC key line.")
    return key


# The point check lives in wallet.py so consensus (registry.py) and this tool use ONE function.
from wallet import is_good_ed25519_point as _is_good_point  # noqa: E402


def read_slot(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    m = SLOT_RE.search(text)
    if not m:
        raise SystemExit(f"Could not find the PAUSE_PUBKEY slot line in {path}. Nothing changed.")
    if len(SLOT_RE.findall(text)) != 1:
        raise SystemExit(f"More than one PAUSE_PUBKEY slot line in {path}. Nothing changed.")
    return m.group(1)


def key_id(key: str) -> str:
    import hashlib
    return hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16]


def show(key: str) -> str:
    groups = " ".join(key[i:i + 8] for i in range(0, 64, 8))
    return f"  key      : {groups}\n  key id   : {key_id(key)}"


def write_slot(path: str, key: str) -> None:
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    new, n = SLOT_RE.subn(lambda m: f'PAUSE_PUBKEY = "{key}"{m.group(2)}', text)
    if n != 1:
        raise SystemExit("Expected exactly one slot line. Nothing changed.")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pubkey", nargs="?", help="the PUBLIC key, 64 hex characters")
    ap.add_argument("--check", action="store_true", help="release gate: exit 1 if the slot is empty or invalid")
    ap.add_argument("--status", action="store_true", help="show the slot and exit 0")
    ap.add_argument("--force", action="store_true", help="replace a key that is already set")
    ap.add_argument("--file", default=os.path.join(ROOT, "pause.py"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    current = read_slot(a.file)
    if a.check or a.status:
        if not current:
            print("PAUSE_PUBKEY slot is EMPTY: the pause switch is disabled. Do NOT release like this.")
            print("  Fix: python ops/set_pause_pubkey.py <the founder's PUBLIC key>")
            return 1 if a.check else 0
        try:
            current = validate(current)
        except ValueError as e:
            print(f"PAUSE_PUBKEY slot holds an INVALID value: {e}")
            return 1 if a.check else 0
        print("PAUSE_PUBKEY slot is set:\n" + show(current))
        return 0
    if not a.pubkey:
        ap.error("give the PUBLIC key, or use --check / --status")
    try:
        key = validate(a.pubkey)
    except ValueError as e:
        print(f"Refused: {e}")
        return 2
    if current and current != key and not a.force:
        print("Refused: the slot already holds a different key. Use --force only if you mean to replace it.")
        return 2
    if current == key:
        print("Slot already holds this key. Nothing changed.\n" + show(key))
        return 0
    write_slot(a.file, key)
    print(f"Wrote the PUBLIC key into {a.file}:\n" + show(key))
    print("\nNext: read the key back to the founder and compare it with the paper copy character by character,\n"
          "then run the tests and the pause drill. Nothing has been pushed or deployed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
