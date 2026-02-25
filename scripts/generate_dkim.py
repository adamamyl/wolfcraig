from __future__ import annotations

import argparse
import base64
import grp
import json
import logging
import os
import pwd
import sys
import time
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.constants import DKIM_CRYPTOPERIOD_DAYS, DKIM_SELECTOR, DOMAINS_JSON, EXIM_DKIM, EXIM_GROUP

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate or rotate DKIM keypairs for mail domains.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--quiet", action="store_true")
    g.add_argument("--debug", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Regenerate even if within cryptoperiod")
    p.add_argument(
        "--create-or-renew",
        action="store_true",
        help="Create if missing, rotate if past cryptoperiod (default behaviour)",
    )
    return p.parse_args()


def key_needs_rotation(private_key: Path) -> bool:
    if not private_key.exists():
        return True
    age_days = (time.time() - private_key.stat().st_mtime) / 86400
    if age_days >= DKIM_CRYPTOPERIOD_DAYS:
        log.info("Key is %.0f days old (limit %d), will rotate", age_days, DKIM_CRYPTOPERIOD_DAYS)
        return True
    log.debug("Key is %.0f days old, within cryptoperiod", age_days)
    return False


def get_public_key_b64(private_key_path: Path) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key_bytes = private_key_path.read_bytes()
    private_key = load_pem_private_key(key_bytes, password=None)
    pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(pub_der).decode()


def generate_keypair(domain: str, dry_run: bool, force: bool) -> bool:
    dkim_dir = EXIM_DKIM / domain
    private_key_path = dkim_dir / "private.key"
    public_key_path = dkim_dir / "public.key"

    if not force and not key_needs_rotation(private_key_path):
        log.info("DKIM key for %s is current, skipping", domain)
        return False

    if dry_run:
        log.info("[dry-run] would generate DKIM keypair for %s", domain)
        return True

    dkim_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    private_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    private_key_path.chmod(0o640)
    os.chown(
        private_key_path,
        pwd.getpwnam("root").pw_uid,
        grp.getgrnam(EXIM_GROUP).gr_gid,
    )

    public_key_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pubkey_b64 = base64.b64encode(pub_der).decode()

    log.info(
        'Generated DKIM key for %s\n  DNS record:\n  %s._domainkey.%s. TXT "v=DKIM1; k=rsa; p=%s"',
        domain,
        DKIM_SELECTOR,
        domain,
        pubkey_b64,
    )
    return True


def main() -> None:
    args = parse_args()

    if args.debug:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    if os.geteuid() != 0:
        sys.exit("generate_dkim.py must be run as root")

    config = json.loads(DOMAINS_JSON.read_text())
    mail_domains = [d for d in config["domains"] if d["mail"]]

    for entry in mail_domains:
        generate_keypair(entry["domain"], args.dry_run, args.force)


if __name__ == "__main__":
    main()
