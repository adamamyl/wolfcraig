from __future__ import annotations

import argparse
import filecmp
import json
import logging
import os
import shutil
import smtplib
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jsonschema

from lib.constants import (
    CERT_DEPLOYER_USER,
    DKIM_SELECTOR,
    DOMAINS_JSON,
    DOMAINS_SCHEMA,
    EXIM_CONF_D,
    EXIM_DKIM,
    EXIM_TEMPLATES,
    MAIL_HOST_PREFIX,
    SUDOERS_DIR,
    SYSTEMD_DIR,
    WOLFCRAIG_CONF_DIR,
    WOLFCRAIG_VENV,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
PACKAGES = ["exim4", "jq", "openssl"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Idempotent setup for wolfcraig.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--quiet", action="store_true")
    g.add_argument("--debug", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p.add_argument("--force", action="store_true", help="Overwrite even if unchanged")
    return p.parse_args()


def run_cmd(cmd: list[str], *, dry_run: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(str(c) for c in cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.run(cmd, check=True, **kwargs)  # type: ignore[call-overload, no-any-return]


def load_and_validate_config() -> dict[str, object]:
    config = json.loads(DOMAINS_JSON.read_text())
    config.pop("$schema", None)  # IDE tooling hint, not part of the data model
    schema = json.loads(DOMAINS_SCHEMA.read_text())
    jsonschema.validate(config, schema)
    log.info("Config validated OK")
    return config  # type: ignore[no-any-return]


def check_apparmor(dry_run: bool) -> None:
    result = subprocess.run(["aa-status", "--json"], capture_output=True, text=True)
    if result.returncode == 0:
        log.info("AppArmor is active")
        try:
            status = json.loads(result.stdout)
            profiles = status.get("profiles", {})
            exim_profiles = [k for k in profiles if "exim" in k.lower()]
            if exim_profiles:
                log.info("Exim AppArmor profiles: %s", exim_profiles)
        except json.JSONDecodeError:
            log.debug("aa-status output not JSON, skipping parse")
    else:
        log.info("AppArmor not active or aa-status not available — not a blocker")


def install_packages(dry_run: bool) -> None:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package} ${Status}\n"] + PACKAGES,
        capture_output=True,
        text=True,
    )
    missing = []
    installed_output = result.stdout
    for pkg in PACKAGES:
        if f"{pkg} install ok installed" not in installed_output:
            missing.append(pkg)

    if not missing:
        log.info("All packages already installed: %s", PACKAGES)
        return

    log.info("Installing packages: %s", missing)
    run_cmd(["apt-get", "install", "-y"] + missing, dry_run=dry_run)


def create_cert_deployer_user(dry_run: bool) -> None:
    result = subprocess.run(["id", CERT_DEPLOYER_USER], capture_output=True)
    if result.returncode != 0:
        log.info("Creating system user: %s", CERT_DEPLOYER_USER)
        run_cmd(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                CERT_DEPLOYER_USER,
            ],
            dry_run=dry_run,
        )
    else:
        log.debug("User %s already exists", CERT_DEPLOYER_USER)

    result2 = subprocess.run(["id", "-nG", CERT_DEPLOYER_USER], capture_output=True, text=True)
    if "docker" not in result2.stdout.split():
        log.info("Adding %s to docker group", CERT_DEPLOYER_USER)
        run_cmd(["usermod", "-aG", "docker", CERT_DEPLOYER_USER], dry_run=dry_run)
    else:
        log.debug("%s already in docker group", CERT_DEPLOYER_USER)


def install_sudoers_rule(dry_run: bool) -> None:
    rule = f"{CERT_DEPLOYER_USER} ALL=(root) NOPASSWD: /bin/systemctl reload exim4\n"
    sudoers_file = SUDOERS_DIR / CERT_DEPLOYER_USER

    if sudoers_file.exists() and sudoers_file.read_text() == rule:
        log.debug("Sudoers rule already installed")
        return

    log.info("Installing sudoers rule: %s", sudoers_file)
    if not dry_run:
        sudoers_file.write_text(rule)
        sudoers_file.chmod(0o440)
    else:
        log.info("[dry-run] would write %s with mode 0o440", sudoers_file)


def create_wolfcraig_venv(dry_run: bool) -> None:
    python_bin = WOLFCRAIG_VENV / "bin" / "python3"
    if python_bin.exists() and not dry_run:
        log.debug("Venv already exists at %s", WOLFCRAIG_VENV)
        return

    log.info("Creating venv at %s", WOLFCRAIG_VENV)
    run_cmd(["uv", "venv", str(WOLFCRAIG_VENV)], dry_run=dry_run)

    run_cmd(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(WOLFCRAIG_VENV / "bin" / "python3"),
            "-e",
            str(REPO_ROOT),
        ],
        dry_run=dry_run,
    )


def _stamp_template(template_path: Path, variables: dict[str, str]) -> str:
    return Template(template_path.read_text()).substitute(variables)


def _validate_exim_config(config_text: str, tmp_path: Path) -> bool:
    tmp_path.write_text(config_text)
    result = subprocess.run(
        ["exim", "-bV", "-C", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("Exim config validation failed:\n%s", result.stderr)
        return False
    return True


def configure_exim(config: dict[str, object], dry_run: bool, force: bool) -> None:
    domains_list = [d for d in config["domains"] if isinstance(d, dict)]  # type: ignore[attr-defined]
    mail_domains = [str(d["domain"]) for d in domains_list if d.get("mail")]

    primary_domain = mail_domains[0] if mail_domains else "localhost"
    primary_hostname = (
        f"{MAIL_HOST_PREFIX}.{primary_domain}"
        if domains_list[0].get("mailsubdomain")
        else primary_domain
    )

    result = subprocess.run(
        ["docker", "network", "inspect", "ghost_network"],
        capture_output=True,
        text=True,
    )
    relay_subnet = "172.18.0.0/16"
    if result.returncode == 0:
        try:
            network_info = json.loads(result.stdout)
            relay_subnet = network_info[0]["IPAM"]["Config"][0]["Subnet"]
        except (json.JSONDecodeError, KeyError, IndexError):
            log.warning("Could not determine Docker bridge subnet, using default %s", relay_subnet)

    variables = {
        "primary_hostname": primary_hostname,
        "primary_domain": primary_domain,
        "relay_subnet": relay_subnet,
        "dkim_base": str(EXIM_DKIM),
        "dkim_selector": DKIM_SELECTOR,
        "mail_host_prefix": MAIL_HOST_PREFIX,
    }

    template_map = {
        "00_local_settings.tpl": EXIM_CONF_D / "main" / "00_wolfcraig_local_settings",
        "30_smtp_outbound.tpl": EXIM_CONF_D / "transport" / "30_wolfcraig_smtp_outbound",
        "200_send_outbound.tpl": EXIM_CONF_D / "router" / "200_wolfcraig_send_outbound",
    }

    changed = False
    for tpl_name, dst in template_map.items():
        tpl_path = EXIM_TEMPLATES / tpl_name
        stamped = _stamp_template(tpl_path, variables)

        if dst.exists() and not force:
            existing = dst.read_text()
            if existing == stamped:
                log.debug("Exim config unchanged: %s", dst)
                continue

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = Path(tf.name)
        valid = _validate_exim_config(stamped, tmp)
        tmp.unlink(missing_ok=True)
        if not valid:
            log.error("Aborting: invalid Exim config for %s", tpl_name)
            sys.exit(1)

        log.info("Installing exim config: %s", dst)
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(stamped)
            os.chmod(dst, 0o644)
        else:
            log.info("[dry-run] would write %s", dst)
        changed = True

    if changed:
        log.info(
            "Exim config updated — run 'update-exim4.conf && invoke-rc.d exim4 restart' to apply"
        )


def generate_dkim_keys(config: dict[str, object], dry_run: bool, force: bool) -> None:
    from scripts.generate_dkim import generate_keypair

    domains_list = config["domains"]
    if not isinstance(domains_list, list):
        return
    mail_domains = [d for d in domains_list if isinstance(d, dict) and d.get("mail")]
    for entry in mail_domains:
        generate_keypair(str(entry["domain"]), dry_run=dry_run, force=force)


def install_systemd_units(dry_run: bool, force: bool) -> None:
    units = [
        "caddy-cert-deploy.service",
        "caddy-cert-deploy.timer",
    ]
    changed = False
    for unit in units:
        src = REPO_ROOT / "systemd" / unit
        dst = SYSTEMD_DIR / unit
        if dst.exists() and not force and filecmp.cmp(str(src), str(dst), shallow=False):
            log.debug("Systemd unit unchanged: %s", unit)
            continue
        log.info("Installing systemd unit: %s", unit)
        if not dry_run:
            shutil.copy2(src, dst)
            os.chmod(dst, 0o644)
        else:
            log.info("[dry-run] would install %s → %s", src, dst)
        changed = True

    if changed:
        run_cmd(["systemctl", "daemon-reload"], dry_run=dry_run)
        run_cmd(
            ["systemctl", "enable", "--now", "caddy-cert-deploy.timer"],
            dry_run=dry_run,
        )


def start_ghost(ghost_compose_path: str, dry_run: bool) -> None:
    compose_file = Path(ghost_compose_path) / "docker-compose.yml"
    run_cmd(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"],
        dry_run=dry_run,
    )
    log.info("Ghost started")


def _read_dkim_b64(domain: str) -> str:
    from scripts.generate_dkim import get_public_key_b64

    key_path = EXIM_DKIM / domain / "private.key"
    if not key_path.exists():
        return "<key not yet generated>"
    return get_public_key_b64(key_path)


def _mta_sts_id() -> str:
    import time

    return str(int(time.time()))


def sync_dns(config: dict[str, object], dry_run: bool) -> None:
    from lib import dns_check, gcp_dns
    from lib.host_info import get_public_ipv4, get_public_ipv6

    creds_file = Path(
        os.environ.get("GCP_DNS_CREDENTIALS_FILE", str(WOLFCRAIG_CONF_DIR / "gcp-dns-sa.json"))
    )
    mta_sts_id = _mta_sts_id()

    domains_list = config["domains"]
    if not isinstance(domains_list, list):
        return

    manual_results: list[dns_check.DomainCheckResult] = []

    for entry in domains_list:
        if not isinstance(entry, dict):
            continue
        domain = str(entry["domain"])
        management = str(entry.get("dns_management", "manual"))

        if management == "gcp":
            dkim_b64 = _read_dkim_b64(domain)
            log.info("Syncing GCP DNS for %s", domain)
            gcp_dns.sync_domain(
                entry,
                dkim_b64,
                mta_sts_id,
                creds_file,
                dry_run=dry_run,
            )
        else:
            log.warning(
                "⚠  DNS for %s is manual — add records at your registrar. See checklist below.",
                domain,
            )
            try:
                server_ipv4 = get_public_ipv4()
                server_ipv6 = get_public_ipv6()
                result = dns_check.check_domain(entry, server_ipv4, server_ipv6)
                manual_results.append(result)
            except RuntimeError as exc:
                log.warning("Could not determine server IPs for DNS check: %s", exc)

    if manual_results:
        dns_check.print_results(manual_results)


def check_dns(config: dict[str, object]) -> None:
    from lib import dns_check
    from lib.host_info import get_public_ipv4, get_public_ipv6

    try:
        server_ipv4 = get_public_ipv4()
        server_ipv6 = get_public_ipv6()
    except RuntimeError as exc:
        log.warning("Could not determine server IPs — skipping DNS validation: %s", exc)
        return

    domains_list = config["domains"]
    if not isinstance(domains_list, list):
        return

    results = [
        dns_check.check_domain(entry, server_ipv4, server_ipv6)
        for entry in domains_list
        if isinstance(entry, dict)
    ]
    dns_check.print_results(results)

    if any(not r.all_ok for r in results):
        log.warning("⚠  Some DNS records are missing or incorrect — see checklist above")


def deploy_certs(dry_run: bool, force: bool) -> None:
    from scripts.deploy_certs import deploy_domain, get_volume_mountpoint, reload_exim
    from lib.constants import ACME_SUBPATH

    config = json.loads(DOMAINS_JSON.read_text())
    mail_domains = [d for d in config["domains"] if d["mail"]]

    try:
        from lib.constants import GHOST_COMPOSE

        mountpoint = get_volume_mountpoint(GHOST_COMPOSE)
    except Exception as exc:
        log.warning("Could not get Caddy volume mountpoint: %s — skipping cert deploy", exc)
        return

    caddy_certs = mountpoint / ACME_SUBPATH
    reload_needed = False
    for entry in mail_domains:
        domain = entry["domain"]
        mail_host = f"{MAIL_HOST_PREFIX}.{domain}" if entry["mailsubdomain"] else domain
        if deploy_domain(domain, mail_host, caddy_certs, dry_run=dry_run, force=force):
            reload_needed = True

    if reload_needed:
        reload_exim(dry_run=dry_run)


def send_test_emails(config: dict[str, object], dry_run: bool) -> None:
    domains_list = config["domains"]
    if not isinstance(domains_list, list):
        return

    mail_domains = [d for d in domains_list if isinstance(d, dict) and d.get("mail")]

    for entry in mail_domains:
        domain = str(entry["domain"])
        from_addr = f"test@{domain}"
        to_addr = f"test@{domain}"

        if dry_run:
            log.info("[dry-run] would send test email from %s", from_addr)
            continue

        msg = EmailMessage()
        msg["Subject"] = f"wolfcraig test email — {domain}"
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(f"Test email from wolfcraig setup for {domain}.")

        try:
            with smtplib.SMTP("localhost", 25) as smtp:
                smtp.send_message(msg)
                log.info("Test email sent from %s (Message-ID: %s)", from_addr, msg["Message-ID"])
        except Exception as exc:
            log.warning("Could not send test email for %s: %s", domain, exc)


def _load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        log.warning(".env not found at %s — GCP DNS sync will be skipped", env_file)
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip()


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("setup.py must be run as root")

    args = parse_args()

    if args.debug:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    _load_env()

    config = load_and_validate_config()

    check_apparmor(args.dry_run)
    install_packages(args.dry_run)
    create_cert_deployer_user(args.dry_run)
    install_sudoers_rule(args.dry_run)
    create_wolfcraig_venv(args.dry_run)
    configure_exim(config, args.dry_run, args.force)
    generate_dkim_keys(config, args.dry_run, args.force)
    install_systemd_units(args.dry_run, args.force)
    start_ghost(str(config["ghost_compose_path"]), args.dry_run)
    sync_dns(config, args.dry_run)
    check_dns(config)
    deploy_certs(args.dry_run, args.force)
    send_test_emails(config, args.dry_run)

    log.info("wolfcraig setup complete")


if __name__ == "__main__":
    main()
