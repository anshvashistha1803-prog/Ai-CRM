"""
Script to generate RSA key pair for JWT signing.
Run this ONCE before starting the application.

Usage:
    python scripts/generate_keys.py

Keys are saved to backend/keys/ (listed in .gitignore — never commit them).
"""

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


def generate_rsa_keys(keys_dir: Path) -> None:
    keys_dir.mkdir(parents=True, exist_ok=True)

    private_key_path = keys_dir / "private.pem"
    public_key_path = keys_dir / "public.pem"

    if private_key_path.exists():
        print(f"[SKIP] Private key already exists at {private_key_path}")
        print("       Delete it first if you want to rotate keys.")
        return

    print("Generating 2048-bit RSA key pair for JWT signing...")

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Write private key (PEM, no passphrase — protect via file permissions)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_path.write_bytes(private_pem)
    os.chmod(private_key_path, 0o600)  # owner read-only
    print(f"[OK] Private key written to {private_key_path} (chmod 600)")

    # Write public key
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_path.write_bytes(public_pem)
    print(f"[OK] Public key written to {public_key_path}")
    print()
    print("Next steps:")
    print("  1. Add to .env:")
    print(f"     JWT_PRIVATE_KEY_PATH={private_key_path.resolve()}")
    print(f"     JWT_PUBLIC_KEY_PATH={public_key_path.resolve()}")


if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    generate_rsa_keys(repo_root / "backend" / "keys")
