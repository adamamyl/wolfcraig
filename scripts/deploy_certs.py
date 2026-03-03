from __future__ import annotations

import argparse
import filecmp
import grp
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import docker as docker_sdk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.constants import (
    ACME_SUBPATH,
    DOMAINS_JSON,
    EXIM_CERTS,
    EXIM_GROUP,
    GHOST_COMPOSE,
    MAIL_HOST_PREFIX,
)

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deploy Caddy-managed TLS certs to Exim.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--quiet", action="store_true")
    g.add_argument("--debug", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p.add_argument("--force", action="store_true", help="Deploy even if certs unchanged")
    return p.parse_args()


def run_cmd(cmd: list[str], *, dry_run: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(str(c) for c in cmd))
        return subprocess.CompletedProcess(cmd, 0)
    return subprocess.run(cmd, check=True, **kwargs)  # type: ignore[call-overload, no-any-return]


def get_volume_mountpoint(compose_file: Path) -> Path:
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    project_name = json.loads(result.stdout)["name"]
    volume_name = f"{project_name}_caddy_data"

    client = docker_sdk.from_env()
    volume = client.volumes.get(volume_name)
    mountpoint = volume.attrs["Mountpoint"]
    if not mountpoint:
        raise RuntimeError(f"Docker SDK returned empty Mountpoint for {volume_name}")
    return Path(mountpoint)


def cert_changed(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return not filecmp.cmp(str(src), str(dst), shallow=False)


def install_cert(src: Path, dst: Path, dry_run: bool) -> None:
    if dry_run:
        log.info("[dry-run] would install %s → %s (640 root:%s)", src, dst, EXIM_GROUP)
        return
    shutil.copy2(src, dst)
    os.chmod(dst, 0o640)
    os.chown(
        dst,
        pwd.getpwnam("root").pw_uid,
        grp.getgrnam(EXIM_GROUP).gr_gid,
    )


def deploy_domain(
    domain: str,
    mail_host: str,
    caddy_certs: Path,
    dry_run: bool,
    force: bool,
) -> bool:
    src_dir = caddy_certs / mail_host
    dst_dir = EXIM_CERTS / domain
    dst_dir.mkdir(parents=True, exist_ok=True)

    changed = False
    pairs = [
        (src_dir / f"{mail_host}.crt", dst_dir / "cert.pem"),
        (src_dir / f"{mail_host}.key", dst_dir / "key.pem"),
    ]

    for src, dst in pairs:
        if not src.exists():
            log.warning("Source cert not found, skipping: %s", src)
            continue
        if force or cert_changed(src, dst):
            install_cert(src, dst, dry_run)
            log.info("Updated %s → %s", src.name, dst)
            changed = True
        else:
            log.debug("No change: %s", dst)

    return changed


def reload_exim(dry_run: bool) -> None:
    run_cmd(["sudo", "systemctl", "reload", "exim4"], dry_run=dry_run)
    log.info("Exim reloaded")


def main() -> None:
    args = parse_args()

    if args.debug:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    config = json.loads(DOMAINS_JSON.read_text())
    mail_domains = [d for d in config["domains"] if d["mail"]]

    mountpoint = get_volume_mountpoint(GHOST_COMPOSE)
    caddy_certs = mountpoint / ACME_SUBPATH

    reload_needed = False
    for entry in mail_domains:
        domain = entry["domain"]
        mail_host = f"{MAIL_HOST_PREFIX}.{domain}" if entry["mailsubdomain"] else domain
        if deploy_domain(domain, mail_host, caddy_certs, args.dry_run, args.force):
            reload_needed = True

    if reload_needed:
        reload_exim(args.dry_run)
    else:
        log.info("All certs up to date, nothing to do")


if __name__ == "__main__":
    main()
