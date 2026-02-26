from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAINS_JSON = REPO_ROOT / "config" / "domains.json"
DOMAINS_SCHEMA = REPO_ROOT / "config" / "domains.schema.json"

EXIM_CERTS = Path("/etc/exim4/certs")
EXIM_CONF_D = Path("/etc/exim4/conf.d")
EXIM_DKIM = Path("/etc/exim4/dkim")
EXIM_TEMPLATES = REPO_ROOT / "exim" / "templates"

SYSTEMD_DIR = Path("/etc/systemd/system")
SUDOERS_DIR = Path("/etc/sudoers.d")
WOLFCRAIG_VENV = Path("/usr/local/lib/wolfcraig-venv")
WOLFCRAIG_CONF_DIR = Path("/etc/wolfcraig")

GHOST_COMPOSE = Path("/opt/ghost-docker/compose.yml")
CADDY_SITES = REPO_ROOT / "caddy" / "sites"

ACME_SUBPATH = "caddy/certificates/acme-v02.api.letsencrypt.org-directory"

DKIM_CRYPTOPERIOD_DAYS = 47
DKIM_SELECTOR = "wolfmail"
MAIL_HOST_PREFIX = "wolfmail"

CERT_DEPLOYER_USER = "cert-deployer"
EXIM_GROUP = "Debian-exim"
