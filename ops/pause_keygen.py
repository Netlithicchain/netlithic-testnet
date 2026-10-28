"""Generate the pause keypair. RUN ONLY ON AN OFFLINE COMPUTER (founder-only procedure, kept private).

Prints the private key once to the screen (write it on paper/metal) and the
public key (the value for PAUSE_PUBKEY). Writes nothing to disk.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from wallet import pubkey_from_private  # noqa: E402


def main():
    if input("Is this computer OFFLINE right now? type YES: ").strip() != "YES":
        sys.exit("Stopped. Disconnect from the network first.")
    sk = Ed25519PrivateKey.generate()
    priv = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption()).hex()
    print("\nPRIVATE KEY (write on paper/metal, never type it online):\n  " + priv)
    print("\nPUBLIC KEY  (share this, it goes in PAUSE_PUBKEY):\n  " + pubkey_from_private(priv))
    input("\nPress Enter after you have written both down; then clear the screen (history -c; clear).")


if __name__ == "__main__":
    main()
