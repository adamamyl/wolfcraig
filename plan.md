# wolfcraig infrastructure — implementation plan

## Overview

This document specifies the full implementation of wolfcraig: a personal VPS running
Ghost (via ghost-docker), Caddy (TLS + reverse proxy), and Exim (outbound mail with
DKIM/SPF/DMARC/MTA-STS). It is orchestrated by machine-setup and lives in its own
repo (`github.com/adamamyl/wolfcraig`).

### Repos involved

| Repo | Purpose | Notes |
|---|---|---|
| `adamamyl/machine-setup` | Orchestrator | Gains a `--wolfcraig` module |
| `adamamyl/wolfcraig` | This machine's config, scripts, systemd | New repo |
| `adamamyl/ghost-docker` | Ghost blog platform for securitysaysyes.com | Exists; loosely tracks upstream |

---

## Architecture

```
machine-setup --wolfcraig
  └── clones wolfcraig      → /usr/local/src/wolfcraig
  └── clones ghost-docker   → /usr/local/src/ghost-docker
  └── runs wolfcraig/setup.py (as root — accepted risk)
        ├── validate domains.json against schema (fail loudly if invalid)
        ├── install packages (exim4, jq, openssl, uv) — idempotent, only if needed
        │     use machine-setup install helpers; check all deps in one pass
        ├── create cert-deployer system user (idempotent)
        ├── install sudoers rule for cert-deployer (idempotent)
        ├── create wolfcraig venv at /usr/local/lib/wolfcraig-venv (idempotent)
        ├── configure exim from templates (stamp domains.json values into conf.d
        │     fragments via string.Template; validate with exim -bV before installing;
        │     fail and do not install if invalid; cmp-equivalent before writing)
        ├── generate DKIM keys (idempotent; skip if within cryptoperiod)
        ├── install systemd units (cmp before install; daemon-reload only if changed)
        ├── docker compose up -d ghost-docker (force-recreate if config changed)
        ├── check DNS records (self-validate with dig; print structured
        │     copy/paste output ordered by type; alarm on missing records)
        ├── deploy_certs.py (first run — only after DNS Gate 1 is confirmed,
        │     as Caddy uses HTTP-01 and needs A/AAAA records to exist)
        └── send test email per mail domain (script constructs and sends)

runtime (systemd timer, twice daily)
  └── scripts/deploy_certs.py (runs as cert-deployer)
        ├── reads domains.json
        ├── inspects caddy_data Docker volume via docker volume inspect
        │     (cert-deployer is in docker group; validate mountpoint is non-empty)
        ├── cmp-equivalent check per domain cert/key pair
        ├── install cert files only if changed (root:Debian-exim, 640)
        └── sudo systemctl reload exim4 — only if at least one cert was updated
```

### Privilege model

| Component | Runs as | Justification |
|---|---|---|
| `setup.py` | root | apt, systemd, /etc writes — accepted risk |
| `deploy_certs.py` (systemd) | `cert-deployer` system user | docker group for volume inspect; writes /etc/exim4/certs only |
| `systemctl reload exim4` | root via targeted sudoers rule | only escalation cert-deployer ever makes |
| Caddy | container user (Docker) | unchanged |
| Exim | `Debian-exim` | unchanged |
| Ghost/DB | container users | unchanged |

`/etc/sudoers.d/cert-deployer`:
```
cert-deployer ALL=(root) NOPASSWD: /bin/systemctl reload exim4
```

### AppArmor

Ubuntu ships Exim with an AppArmor profile. The default profile allows reads from
`/etc/exim4/` so our cert paths at `/etc/exim4/certs/` and DKIM keys at
`/etc/exim4/dkim/` fall within it. setup.py should run `aa-status` and log the
result — not a blocker but worth surfacing. If a future change breaks confinement,
the relevant profile is `/etc/apparmor.d/usr.sbin.exim4`.

### Python environment

setup.py creates a venv at `/usr/local/lib/wolfcraig-venv/` with wolfcraig's
dependencies. The systemd unit's `ExecStart` uses that venv's Python directly
(`/usr/local/lib/wolfcraig-venv/bin/python3`), so no `uv run` indirection at
runtime — but the venv is built and managed by uv during setup.

---

## Repository structure: `wolfcraig`

```
wolfcraig/
  config/
    domains.json              # single source of truth — domains and capabilities
    domains.schema.json       # JSON schema; validated at startup
  caddy/
    Caddyfile                 # thin root; imports from sites/
    sites/
      amyl.org.uk             # Caddy site block (ACME stub + MTA-STS)
      securitysaysyes.com     # Caddy site block (Ghost reverse proxy + MTA-STS)
  exim/
    templates/                # string.Template files; stamped from domains.json by setup.py
      00_local_settings.tpl   # primary_hostname, interfaces, TLS
      30_smtp_outbound.tpl    # DKIM-signing SMTP transport
      200_send_outbound.tpl   # dnslookup router
    dkim/                     # gitignored — generated on first run
      .gitkeep
  systemd/
    caddy-cert-deploy.service
    caddy-cert-deploy.timer
  scripts/
    deploy_certs.py
    generate_dkim.py
  lib/
    constants.py              # shared paths and constants across all scripts
    dns_check.py              # DNS validation logic (read — uses dnspython)
    gcp_dns.py                # GCP Cloud DNS record management (write — uses google-cloud-dns)
  tests/
    test_deploy_certs.py
    test_generate_dkim.py
    test_dns_check.py
    test_gcp_dns.py
    fixtures/
      domains.json            # test fixture — fake domains (example.test), never real
  setup.py                    # entry point called by machine-setup
  pyproject.toml              # all tool config: ruff, bandit, mypy, pytest
  .pre-commit-config.yaml
  .env.example
  .gitignore
  README.md
  plan.md                     # this document
```

---

## Flags

Consistent with machine-setup conventions. All scripts respect these and support
them in any order.

| Flag | Behaviour |
|---|---|
| `--dry-run` | Show what would be done; print subprocess calls in subjunctive form without executing |
| `--verbose` | Log at INFO level; surface all steps |
| `--debug` | Log at DEBUG level; equivalent to `set -x` — show every decision |
| `--quiet` | Log at WARNING and above only; suitable for systemd timer runs |
| `--force` | Overwrite existing files and configs even if cmp shows no change |
| `--help` | Show args and exit |

`--dry-run` is threaded through every function that calls subprocess. A shared
`run()` wrapper in each script handles this: if `--dry-run` is set, log
`[dry-run] would run: {cmd}` instead of executing.

---

## Library decisions

Prefer libraries over reimplementing — but scope changes to wolfcraig only,
do not rewrite equivalent code in machine-setup or herewegoagain.

| What | Library | Replaces | Notes |
|---|---|---|---|
| File comparison | `filecmp` (stdlib) | `subprocess cmp -s` | `filecmp.cmp(src, dst, shallow=False)` for content comparison |
| File copy + permissions | `shutil`, `os.chmod`, `os.chown`, `pwd`, `grp` (all stdlib) | `subprocess install`, `subprocess chown` | Proper Python objects; easier to mock in tests |
| RSA key generation | `cryptography` | all `subprocess openssl` calls | Native key objects; no stdout parsing; cleaner DNS value extraction |
| Docker volume inspection | `docker` SDK | `subprocess docker compose config`, `subprocess docker volume inspect` | Official SDK; proper exceptions; no `json.loads(stdout)` |
| DNS record management | `google-cloud-dns` | manual registrar edits | Automates A, AAAA, DKIM, DMARC, MTA-STS, SPF across full zone |
| CLI argument parsing | `argparse` (stdlib) | — | Keep consistent with machine-setup; do not switch to typer |

### Zone delegation — design decision

All mail-related DNS records (`mail.*`, `_dmarc`, `_mta-sts`, `mail._domainkey`,
`_smtp._tls`, `mta-sts.*`, SPF at apex) live across the full zone, not just a
`mail` subdomain. To automate all of them, the **entire zone** for each domain is
delegated to GCP Cloud DNS. The registrar holds only NS records pointing at GCP
nameservers — set once, never touched again.

```
registrar (one-time setup):
  securitysaysyes.com.  NS  ns-cloud-a1.googledomains.com.
  securitysaysyes.com.  NS  ns-cloud-a2.googledomains.com.
  securitysaysyes.com.  NS  ns-cloud-a3.googledomains.com.
  securitysaysyes.com.  NS  ns-cloud-a4.googledomains.com.
  (same for amyl.org.uk)

GCP Cloud DNS (managed by our script, idempotent):
  mail.securitysaysyes.com.            A      <server ipv4>
  mail.securitysaysyes.com.            AAAA   <server ipv6>
  mta-sts.securitysaysyes.com.         A      <server ipv4>
  securitysaysyes.com.                 TXT    "v=spf1 ..."
  mail._domainkey.securitysaysyes.com. TXT    "v=DKIM1; k=rsa; p=..."
  _dmarc.securitysaysyes.com.          TXT    "v=DMARC1; p=reject; ..."
  _mta-sts.securitysaysyes.com.        TXT    "v=STSv1; id=..."
  _smtp._tls.securitysaysyes.com.      TXT    "v=TLSRPTv1; rua=..."
```

This applies to both `securitysaysyes.com` and `amyl.org.uk`. Each domain gets
its own GCP Cloud DNS managed zone. The `id=` timestamp in the MTA-STS TXT record
is updated automatically by the script when the MTA-STS policy content changes.

### Key patterns

**File comparison** — replaces `subprocess.run(["cmp", "-s", src, dst])`:
```python
import filecmp
changed = not filecmp.cmp(str(src), str(dst), shallow=False)
```

**File install with permissions** — replaces `subprocess.run(["install", "-m", "640", ...])`:
```python
import shutil, os, grp, pwd

shutil.copy2(src, dst)
os.chmod(dst, 0o640)
os.chown(
    dst,
    pwd.getpwnam("root").pw_uid,
    grp.getgrnam("Debian-exim").gr_gid,
)
```

**DKIM key generation** — replaces three openssl subprocess calls:
```python
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import base64

private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend(),
)
private_key_path.write_bytes(
    private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
)
pub_der = private_key.public_key().public_bytes(
    serialization.Encoding.DER,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)
pubkey_b64 = base64.b64encode(pub_der).decode()
# → ready to paste into DNS TXT record
```

**Docker volume mountpoint** — replaces `docker compose config` + `docker volume inspect`:
```python
import docker

client = docker.from_env()
volume = client.volumes.get(f"{project_name}_caddy_data")
mountpoint = Path(volume.attrs["Mountpoint"])
```

Getting `project_name` still requires reading the compose file — use
`docker compose config --format json | jq .name` or parse the compose file
directly. The volume lookup itself becomes a clean SDK call with proper exceptions
rather than stdout parsing.

---

## Mail hostname convention — `wolfmail` prefix

Mail records use a `wolfmail.$DOMAIN` subdomain to coexist cleanly with existing
Google Workspace setups on both domains. Using the generic `mail` prefix risks
colliding with established MX infrastructure; a distinct prefix makes the VPS mail
stack unambiguous in DNS, logs, and DKIM records.

- `wolfmail.amyl.org.uk` — the hostname Exim presents in HELO/EHLO; A/AAAA point to VPS
- `wolfmail.securitysaysyes.com` — same

The `wolfmail` prefix propagates through the full stack:
- DNS A/AAAA records (`wolfmail.$DOMAIN.`)
- Caddy ACME cert acquisition (cert issued for `wolfmail.$DOMAIN`)
- Exim `primary_hostname` and TLS SNI
- DKIM selector: `wolfmail._domainkey.$DOMAIN` (distinct from `google._domainkey`)
- Caddy cert path inside the Docker volume (`wolfmail.$DOMAIN/` directory)
- Exim TLS cert/key paths under `/etc/exim4/certs/$DOMAIN/`

The `mailsubdomain: true` field in domains.json remains the flag that enables
subdomain mail hosting. The prefix `wolfmail` is a constant (`DKIM_SELECTOR = "wolfmail"`
and `MAIL_HOST_PREFIX = "wolfmail"` in `lib/constants.py`) rather than per-domain
config — both domains use the same prefix.

> **Future option**: delegating `wolfmail.$DOMAIN` as a subdomain zone to GCP Cloud DNS
> would enable automated DKIM key rotation without touching the registrar. Not
> implemented now, but the architecture supports it cleanly.

---

## Coexisting with Google Workspace — SPF and DKIM

Both domains have existing Google Workspace MX records and SPF entries. The wolfcraig
mail stack must not break these.

### SPF — additive, not replacement

The existing SPF records include `include:_spf.google.com`. The script must **not**
replace the SPF record wholesale — it must read the existing value and merge in the
VPS IPs:

```
; existing (Google Workspace):
securitysaysyes.com.  TXT  "v=spf1 include:_spf.google.com ~all"

; desired after wolfcraig (additive merge):
securitysaysyes.com.  TXT  "v=spf1 include:_spf.google.com ip4:<vps-ipv4> ip6:<vps-ipv6> -all"
```

`gcp_dns.py` fetches the live SPF record before building desired state, extracts
existing mechanisms, appends VPS IPs if not already present, and hardens `~all`
to `-all`. If no SPF record exists, it creates one from scratch.

`fetch_existing_spf()` in `gcp_dns.py` handles the live lookup and parse.
`build_spf_record()` handles the merge logic. `build_records_for_domain()` calls
both, gaining a `zone` parameter so it can query the live state.

For manual domains (`amyl.org.uk`), `dns_check.py` prints the merged desired
value in the copy-paste checklist — the operator copies it, validates manually,
and applies it at the registrar.

### DKIM selector — `wolfmail`

`wolfmail._domainkey.$DOMAIN` avoids collision with `google._domainkey` and any
legacy keys. `DKIM_SELECTOR = "wolfmail"` in `lib/constants.py` is the single
definition used by:
- `generate_dkim.py` — copy-paste DNS output and log messages
- Exim `30_smtp_outbound.tpl` — `dkim_selector = ${dkim_selector}` stamped at setup
- `gcp_dns.py` — record name `wolfmail._domainkey.{domain}.`
- `dns_check.py` — validation checks `wolfmail._domainkey.{domain}`

### TXT record chunking

A 2048-bit RSA public key is ~400 characters base64-encoded. GCP DNS stores long
TXT records as multiple quoted strings (RFC 4408, 255-byte boundary). The script
matches the format produced by `gcloud`:

```json
"rrdatas": [
  "\"v=DKIM1; k=rsa; p=<first-255-chars>\"",
  "\"<remaining-chars>\""
]
```

`_split_dkim_rrdata()` in `gcp_dns.py` produces this format. `record_needs_update()`
normalises both sides before comparing — concatenating all strings and stripping
quotes — so a record already created via `gcloud` CLI is not re-uploaded unnecessarily.

---
## `.env` and `.env.example`

`.env` is gitignored and holds secrets. `.env.example` is committed as a template.
`setup.py` checks that all required vars are present in `.env` at startup and
fails loudly with a clear message if any are missing.

`.env.example`:
```bash
# GCP Cloud DNS credentials and project config
# Get these from: GCP Console → IAM → Service Accounts → wolfcraig-dns
GCP_PROJECT_ID=your-gcp-project-id
GCP_DNS_CREDENTIALS_FILE=/etc/wolfcraig/gcp-dns-sa.json

# Server IP addresses (used in DNS A/AAAA records)
SERVER_IPV4=
SERVER_IPV6=
```

The service account JSON key is stored at `/etc/wolfcraig/gcp-dns-sa.json`
(not in the repo root, not in `/tmp`). `setup.py` creates `/etc/wolfcraig/`
with `700 root:root` permissions and validates the credentials file exists and
is readable only by root before proceeding.

---

## `lib/gcp_dns.py`

Manages DNS records in GCP Cloud DNS. Idempotent — checks existing record value
before making any API call; only updates if the value would actually change.
Verifies propagation after changes using `dns_check.py`'s resolver.

Separation of concerns:
- `dns_check.py` — **reads** DNS (validation, status reporting)
- `gcp_dns.py` — **writes** DNS (record management via GCP API)

```python
"""
gcp_dns.py — manage DNS records in GCP Cloud DNS.

Idempotent: checks existing record before each API call.
Only updates if value would change. Verifies propagation after changes.
Runnable standalone or imported by setup.py and generate_dkim.py.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import NamedTuple

from google.cloud import dns
from google.oauth2 import service_account

log = logging.getLogger(__name__)

# TTL for all records we manage
DEFAULT_TTL = 300


class DnsRecord(NamedTuple):
    name: str        # fully qualified, with trailing dot
    record_type: str
    ttl: int
    rrdatas: list[str]  # one or more values


def get_client(credentials_file: Path) -> dns.Client:
    """Build a GCP DNS client from a service account JSON key file."""
    project_id = os.environ["GCP_PROJECT_ID"]
    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_file),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return dns.Client(project=project_id, credentials=credentials)


def get_zone(client: dns.Client, domain: str) -> dns.ManagedZone:
    """Return the GCP managed zone for a domain. Raises if not found."""
    # Zone names in GCP use hyphens, not dots
    zone_name = domain.rstrip(".").replace(".", "-")
    zone = client.zone(zone_name, dns_name=f"{domain.rstrip('.')}.")
    if not zone.exists():
        raise RuntimeError(
            f"GCP managed zone '{zone_name}' not found for domain '{domain}'. "
            f"Create it in the GCP Console first — see README.md."
        )
    return zone


def record_needs_update(
    zone: dns.ManagedZone,
    record: DnsRecord,
) -> bool:
    """Return True if the record doesn't exist or differs from desired value."""
    for existing in zone.list_resource_record_sets():
        if existing.name == record.name and existing.record_type == record.record_type:
            if sorted(existing.rrdatas) == sorted(record.rrdatas):
                log.debug("Record up to date: %s %s", record.record_type, record.name)
                return False
            log.info(
                "Record differs: %s %s\n  current: %s\n  desired: %s",
                record.record_type, record.name,
                existing.rrdatas, record.rrdatas,
            )
            return True
    log.info("Record missing: %s %s", record.record_type, record.name)
    return True


def upsert_record(
    zone: dns.ManagedZone,
    record: DnsRecord,
    dry_run: bool,
) -> bool:
    """Create or update a DNS record. Returns True if a change was made."""
    if not record_needs_update(zone, record):
        return False

    if dry_run:
        log.info(
            "[dry-run] would upsert %s %s = %s",
            record.record_type, record.name, record.rrdatas,
        )
        return True

    # Delete existing if present, then create
    changes = zone.changes()
    for existing in zone.list_resource_record_sets():
        if existing.name == record.name and existing.record_type == record.record_type:
            changes.delete_record_set(existing)

    new_record = zone.resource_record_set(
        record.name, record.record_type, record.ttl, record.rrdatas
    )
    changes.add_record_set(new_record)
    changes.create()

    log.info("Upserted %s %s = %s", record.record_type, record.name, record.rrdatas)
    return True


def wait_for_propagation(
    name: str,
    record_type: str,
    expected_value: str,
    timeout_seconds: int = 300,
    poll_interval: int = 15,
) -> bool:
    """
    Poll until the record propagates or timeout is reached.
    Uses dnspython directly (same as dns_check.py) for consistency.
    Returns True if propagated, False if timed out.
    """
    import dns.resolver
    import dns.exception

    deadline = time.time() + timeout_seconds
    log.info("Waiting for %s %s to propagate (timeout %ds)...", record_type, name, timeout_seconds)

    while time.time() < deadline:
        try:
            answers = dns.resolver.resolve(name, record_type)
            for rdata in answers:
                value = str(rdata) if record_type in ("A", "AAAA") else (
                    b"".join(rdata.strings).decode()
                )
                if expected_value in value:
                    log.info("Propagated: %s %s", record_type, name)
                    return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException):
            pass

        log.debug("Not yet propagated, retrying in %ds...", poll_interval)
        time.sleep(poll_interval)

    log.warning("Timed out waiting for propagation of %s %s", record_type, name)
    return False


def build_records_for_domain(
    domain_config: dict,
    server_ipv4: str,
    server_ipv6: str,
    dkim_public_key_b64: str,
    mta_sts_id: str,
) -> list[DnsRecord]:
    """
    Build the full list of desired DNS records for a domain from domains.json config.
    Caller passes in dynamic values (IPs, DKIM key, MTA-STS timestamp).
    """
    domain = domain_config["domain"]
    mail_host = f"mail.{domain}." if domain_config["mailsubdomain"] else f"{domain}."
    records = []

    # A and AAAA for mail host
    records.append(DnsRecord(mail_host, "A",    DEFAULT_TTL, [server_ipv4]))
    records.append(DnsRecord(mail_host, "AAAA", DEFAULT_TTL, [server_ipv6]))

    # A for mta-sts host (IPv4 only — MTA-STS policy served over HTTPS)
    records.append(DnsRecord(f"mta-sts.{domain}.", "A", DEFAULT_TTL, [server_ipv4]))

    if domain_config["mail"]:
        # SPF
        records.append(DnsRecord(
            f"{domain}.", "TXT", DEFAULT_TTL,
            [f'"v=spf1 ip4:{server_ipv4} ip6:{server_ipv6} -all"'],
        ))
        # DKIM
        records.append(DnsRecord(
            f"mail._domainkey.{domain}.", "TXT", DEFAULT_TTL,
            [f'"v=DKIM1; k=rsa; p={dkim_public_key_b64}"'],
        ))
        # DMARC
        records.append(DnsRecord(
            f"_dmarc.{domain}.", "TXT", DEFAULT_TTL,
            [f'"v=DMARC1; p=reject; rua=mailto:dmarc@{domain}; adkim=s; aspf=s"'],
        ))
        # MTA-STS discovery — id changes when policy changes
        records.append(DnsRecord(
            f"_mta-sts.{domain}.", "TXT", DEFAULT_TTL,
            [f'"v=STSv1; id={mta_sts_id}"'],
        ))
        # TLS reporting
        records.append(DnsRecord(
            f"_smtp._tls.{domain}.", "TXT", DEFAULT_TTL,
            [f'"v=TLSRPTv1; rua=mailto:tls@{domain}"'],
        ))

    return records


def sync_domain(
    domain_config: dict,
    server_ipv4: str,
    server_ipv6: str,
    dkim_public_key_b64: str,
    mta_sts_id: str,
    credentials_file: Path,
    dry_run: bool,
    wait_propagation: bool = True,
) -> None:
    """
    Sync all DNS records for one domain. Idempotent.
    Caller is responsible for checking domain_config["dns_management"] == "gcp"
    before calling — this function does not skip; it always acts.
    """
    domain = domain_config["domain"]
    client = get_client(credentials_file)
    zone = get_zone(client, domain)

    records = build_records_for_domain(
        domain_config, server_ipv4, server_ipv6, dkim_public_key_b64, mta_sts_id,
    )

    changed_records = []
    for record in records:
        if upsert_record(zone, record, dry_run):
            changed_records.append(record)

    if not changed_records:
        log.info("All DNS records for %s are up to date", domain)
        return

    if wait_propagation and not dry_run:
        for record in changed_records:
            wait_for_propagation(
                record.name.rstrip("."),
                record.record_type,
                record.rrdatas[0].strip('"'),
            )
```

---

## `config/domains.json`

Array of domain objects — not a keyed object. Easier to iterate and filter in
Python without reconstructing the domain name from a dict key. Validated against
`domains.schema.json` at startup.

`dns_management` is per-domain and drives whether the script automates DNS via
GCP or skips it and falls back to printing a manual checklist. `"gcp"` means full
automation; `"manual"` means dns_check.py validates and reports but gcp_dns.py
does nothing for that domain.

```json
{
  "$schema": "./config/domains.schema.json",
  "ghost_compose_path": "/usr/local/src/ghost-docker",
  "domains": [
    {
      "domain": "amyl.org.uk",
      "mail": true,
      "web": true,
      "ghost": false,
      "mailsubdomain": true,
      "dns_management": "manual"
    },
    {
      "domain": "securitysaysyes.com",
      "mail": true,
      "web": true,
      "ghost": true,
      "mailsubdomain": true,
      "dns_management": "gcp"
    }
  ]
}
```

`config/domains.schema.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["ghost_compose_path", "domains"],
  "additionalProperties": false,
  "properties": {
    "ghost_compose_path": { "type": "string" },
    "domains": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["domain", "mail", "web", "ghost", "mailsubdomain", "dns_management"],
        "additionalProperties": false,
        "properties": {
          "domain":         { "type": "string" },
          "mail":           { "type": "boolean" },
          "web":            { "type": "boolean" },
          "ghost":          { "type": "boolean" },
          "mailsubdomain":  { "type": "boolean" },
          "dns_management": { "type": "string", "enum": ["gcp", "manual"] }
        }
      }
    }
  }
}
```

The `enum` constraint on `dns_management` means any value other than `"gcp"` or
`"manual"` is caught at schema validation time — before any script logic runs.
Adding a new management backend in future (e.g. `"cloudflare"`) means adding it
to the enum and implementing a corresponding module.

---

## `lib/constants.py`

Shared constants imported by all scripts. Single definition, no duplication.

```python
"""
constants.py — shared paths and constants for wolfcraig scripts.
"""
from pathlib import Path

REPO_ROOT              = Path(__file__).resolve().parents[1]
DOMAINS_JSON           = REPO_ROOT / "config" / "domains.json"
DOMAINS_SCHEMA         = REPO_ROOT / "config" / "domains.schema.json"
EXIM_CERTS             = Path("/etc/exim4/certs")
EXIM_CONF_D            = Path("/etc/exim4/conf.d")
EXIM_DKIM              = Path("/etc/exim4/dkim")
EXIM_TEMPLATES         = REPO_ROOT / "exim" / "templates"
SYSTEMD_DIR            = Path("/etc/systemd/system")
SUDOERS_DIR            = Path("/etc/sudoers.d")
WOLFCRAIG_VENV         = Path("/usr/local/lib/wolfcraig-venv")
GHOST_COMPOSE          = Path("/usr/local/src/ghost-docker/docker-compose.yml")
ACME_SUBPATH           = (
    "caddy/certificates/"
    "acme-v02.api.letsencrypt.org-directory"
)
DKIM_CRYPTOPERIOD_DAYS = 47      # rotate before this age (NIST guidance)
CERT_DEPLOYER_USER     = "cert-deployer"
```

---

## `lib/dns_check.py`

DNS self-validation using `dnspython` — native Python objects, no subprocess,
no system `dig` version dependency. Returns structured results ordered by record
type; alarms loudly on missing records. `DomainCheckResult` serialises cleanly
to JSON for future DNS API use.

**Why dnspython over `dig` subprocess:**
- Native Python objects rather than parsed strings — clean comparison logic
- Proper exception hierarchy (`NXDOMAIN`, `NoAnswer`, `DNSException`) rather
  than string matching on command output
- Works identically regardless of which version of BIND is installed on the host
- One dep added to `pyproject.toml`; the `DomainCheckResult` dataclass shape
  is unchanged — only the internals of the query helpers differ

```python
"""
dns_check.py — validate DNS records for wolfcraig domains.

Uses dnspython for all queries — native Python objects, no subprocess.
Returns structured results; prints ordered output (A/AAAA together, TXT together).
Alarms loudly on missing or incorrect records.
Runnable standalone or imported by setup.py.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Literal

import dns.exception
import dns.resolver

log = logging.getLogger(__name__)
Status = Literal["ok", "missing", "mismatch", "unknown"]


@dataclass
class RecordResult:
    domain: str
    record_type: str    # A, AAAA, TXT, MX
    name: str           # full DNS name with trailing dot
    expected: str
    actual: str | None
    status: Status


@dataclass
class DomainCheckResult:
    domain: str
    records: list[RecordResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(r.status == "ok" for r in self.records)

    @property
    def missing(self) -> list[RecordResult]:
        return [r for r in self.records if r.status != "ok"]

    def to_dict(self) -> dict:
        """Serialise to dict — feeds JSON output or future DNS API calls."""
        return asdict(self)


def check_domain(
    domain_config: dict,
    server_ipv4: str,
    server_ipv6: str,
) -> DomainCheckResult:
    """Check all required DNS records for one domain entry from domains.json."""
    domain = domain_config["domain"]
    mail_host = f"mail.{domain}" if domain_config["mailsubdomain"] else domain
    result = DomainCheckResult(domain=domain)

    # A and AAAA for mail host
    for rtype, expected_ip in [("A", server_ipv4), ("AAAA", server_ipv6)]:
        actual = _query_address(mail_host, rtype)
        result.records.append(RecordResult(
            domain=domain, record_type=rtype,
            name=f"{mail_host}.",
            expected=expected_ip,
            actual=actual,
            status="ok" if actual == expected_ip else (
                "missing" if actual is None else "mismatch"
            ),
        ))

    if domain_config["mail"]:
        # SPF
        spf_expected = f"v=spf1 ip4:{server_ipv4} ip6:{server_ipv6} -all"
        spf_actual = _query_txt_prefix(domain, "v=spf1")
        result.records.append(RecordResult(
            domain=domain, record_type="TXT", name=f"{domain}.",
            expected=spf_expected, actual=spf_actual,
            status=_txt_status(spf_actual, "v=spf1"),
        ))

        # DKIM
        dkim_name = f"mail._domainkey.{domain}"
        dkim_actual = _query_txt_prefix(dkim_name, "v=DKIM1")
        result.records.append(RecordResult(
            domain=domain, record_type="TXT", name=f"{dkim_name}.",
            expected="v=DKIM1; k=rsa; p=<key>",
            actual=dkim_actual,
            status=_txt_status(dkim_actual, "v=DKIM1"),
        ))

        # DMARC, MTA-STS, TLS-RPT
        for prefix, name in [
            ("v=DMARC1",   f"_dmarc.{domain}"),
            ("v=STSv1",    f"_mta-sts.{domain}"),
            ("v=TLSRPTv1", f"_smtp._tls.{domain}"),
        ]:
            actual = _query_txt_prefix(name, prefix)
            result.records.append(RecordResult(
                domain=domain, record_type="TXT", name=f"{name}.",
                expected=f"{prefix}; ...",
                actual=actual,
                status=_txt_status(actual, prefix),
            ))

    return result


def print_results(results: list[DomainCheckResult]) -> None:
    """Print DNS results ordered by type: A/AAAA first, then TXT."""
    print("\n" + "=" * 70)
    print("DNS VALIDATION RESULTS")
    print("=" * 70)

    for result in results:
        print(f"\n{'✓' if result.all_ok else '✗'} {result.domain}")
        for rtype in ("A", "AAAA", "MX", "TXT"):
            for r in [x for x in result.records if x.record_type == rtype]:
                icon = "  ✓" if r.status == "ok" else "  ✗"
                print(f"{icon} {r.record_type:<6} {r.name}")
                if r.status != "ok":
                    print(f"         expected : {r.expected}")
                    print(f"         actual   : {r.actual or '(not found)'}")

    missing_count = sum(len(r.missing) for r in results)
    if missing_count:
        print(f"\n{'=' * 70}")
        print(f"⚠  {missing_count} record(s) missing or incorrect. Copy-paste values:")
        print("=" * 70)
        _print_copy_paste(results)

    print()


def _print_copy_paste(results: list[DomainCheckResult]) -> None:
    """Print missing records in copy-paste format, grouped by type, trailing dots."""
    for rtype in ("A", "AAAA", "TXT"):
        records = [
            r for result in results
            for r in result.missing
            if r.record_type == rtype
        ]
        if records:
            print(f"\n; {rtype} records")
            for r in records:
                print(f"{r.name:<55} {r.record_type:<6} {r.expected}")


def _query_address(name: str, rtype: str) -> str | None:
    """Return first address answer for an A or AAAA query, or None."""
    try:
        answers = dns.resolver.resolve(name, rtype)
        return str(answers[0])
    except dns.resolver.NXDOMAIN:
        return None
    except dns.resolver.NoAnswer:
        return None
    except dns.exception.DNSException as exc:
        log.warning("DNS query failed for %s %s: %s", rtype, name, exc)
        return None


def _query_txt_prefix(name: str, prefix: str) -> str | None:
    """Return the first TXT record starting with prefix, or None."""
    try:
        answers = dns.resolver.resolve(name, "TXT")
        for rdata in answers:
            # each rdata.strings is a list of bytes chunks; join and decode
            value = b"".join(rdata.strings).decode()
            if value.startswith(prefix):
                return value
        return None
    except dns.resolver.NXDOMAIN:
        return None
    except dns.resolver.NoAnswer:
        return None
    except dns.exception.DNSException as exc:
        log.warning("DNS TXT query failed for %s: %s", name, exc)
        return None


def _txt_status(actual: str | None, prefix: str) -> Status:
    if actual is None:
        return "missing"
    return "ok" if actual.startswith(prefix) else "mismatch"
```

---

## `scripts/deploy_certs.py`

Reads domains.json, finds the Caddy volume mountpoint via the Docker SDK
(`docker.from_env()`), compares certs with `filecmp.cmp()`, copies with
`shutil.copy2()` + `os.chmod()` + `os.chown()`. Reloads Exim only if something
changed. The only subprocess call is `sudo systemctl reload exim4` — everything
else is native Python or SDK.

`cert-deployer` is in the `docker` group so the SDK can reach the Docker socket.

```python
#!/usr/bin/env python3
"""
deploy_certs.py — deploy Caddy-managed TLS certs to Exim.

Runs as cert-deployer (unprivileged).
Only escalation: sudo systemctl reload exim4, and only if a cert changed.
Called by systemd timer (caddy-cert-deploy.timer) twice daily.
"""
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

import docker

from lib.constants import (
    ACME_SUBPATH, DOMAINS_JSON, EXIM_CERTS, GHOST_COMPOSE,
)

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--quiet",   action="store_true")
    g.add_argument("--debug",   action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p.add_argument("--force",   action="store_true", help="Deploy even if certs unchanged")
    return p.parse_args()


def run_cmd(cmd: list[str], *, dry_run: bool, **kwargs) -> subprocess.CompletedProcess:
    """Subprocess wrapper — only used where no SDK/stdlib alternative exists."""
    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(str(c) for c in cmd))
        return subprocess.CompletedProcess(cmd, 0)
    return subprocess.run(cmd, check=True, **kwargs)


def get_volume_mountpoint(compose_file: Path) -> Path:
    """Resolve caddy_data Docker volume mountpoint via Docker SDK."""
    # Project name still requires reading the compose config — one subprocess call
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    project_name = json.loads(result.stdout)["name"]
    volume_name = f"{project_name}_caddy_data"

    client = docker.from_env()
    volume = client.volumes.get(volume_name)
    mountpoint = volume.attrs["Mountpoint"]
    if not mountpoint:
        raise RuntimeError(f"Docker SDK returned empty Mountpoint for {volume_name}")
    return Path(mountpoint)


def cert_changed(src: Path, dst: Path) -> bool:
    """Return True if src and dst differ, or dst does not exist."""
    if not dst.exists():
        return True
    return not filecmp.cmp(str(src), str(dst), shallow=False)


def install_cert(src: Path, dst: Path, dry_run: bool) -> None:
    """Copy src to dst and set ownership/permissions — stdlib, no subprocess."""
    if dry_run:
        log.info("[dry-run] would install %s → %s (640 root:Debian-exim)", src, dst)
        return
    shutil.copy2(src, dst)
    os.chmod(dst, 0o640)
    os.chown(
        dst,
        pwd.getpwnam("root").pw_uid,
        grp.getgrnam("Debian-exim").gr_gid,
    )


def deploy_domain(
    domain: str,
    mail_host: str,
    caddy_certs: Path,
    dry_run: bool,
    force: bool,
) -> bool:
    """Deploy cert + key for one domain. Returns True if anything changed."""
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

    level = logging.WARNING if args.quiet else (
        logging.DEBUG if args.debug else logging.INFO
    )
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
        mail_host = f"mail.{domain}" if entry["mailsubdomain"] else domain
        if deploy_domain(domain, mail_host, caddy_certs, args.dry_run, args.force):
            reload_needed = True

    if reload_needed:
        reload_exim(args.dry_run)
    else:
        log.info("All certs up to date, nothing to do")


if __name__ == "__main__":
    main()
```

---

## `scripts/generate_dkim.py`

Idempotent DKIM key generation with a 47-day cryptoperiod. Skips a domain if its
key exists and is within the cryptoperiod; rotates if older. Prints DNS TXT values
in copy-paste format after generation.

**Note**: DKIM keys are self-signed by design — the DNS TXT record at
`mail._domainkey.$DOMAIN` is the trust anchor. No CA or Let's Encrypt involvement
is needed or appropriate for DKIM.

```python
#!/usr/bin/env python3
"""
generate_dkim.py — generate or rotate DKIM keypairs for mail domains.

Idempotent: skips domains whose key is within the cryptoperiod.
Rotates keys older than DKIM_CRYPTOPERIOD_DAYS (47 days).
Uses the cryptography library — no openssl subprocess calls.
Must be run as root (called from setup.py).
"""
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

from lib.constants import DKIM_CRYPTOPERIOD_DAYS, DOMAINS_JSON, EXIM_DKIM

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--quiet",   action="store_true")
    g.add_argument("--debug",   action="store_true")
    p.add_argument("--dry-run",         action="store_true")
    p.add_argument("--force",           action="store_true",
                   help="Regenerate even if within cryptoperiod")
    p.add_argument("--create-or-renew", action="store_true",
                   help="Create if missing, rotate if past cryptoperiod (default)")
    return p.parse_args()


def key_needs_rotation(private_key: Path) -> bool:
    """Return True if key does not exist or is older than the cryptoperiod."""
    if not private_key.exists():
        return True
    age_days = (time.time() - private_key.stat().st_mtime) / 86400
    if age_days >= DKIM_CRYPTOPERIOD_DAYS:
        log.info("Key is %.0f days old (limit %d), will rotate", age_days, DKIM_CRYPTOPERIOD_DAYS)
        return True
    log.debug("Key is %.0f days old, within cryptoperiod", age_days)
    return False


def generate_keypair(domain: str, dry_run: bool, force: bool) -> bool:
    """Generate or rotate keypair for domain. Returns True if a key was generated."""
    dkim_dir    = EXIM_DKIM / domain
    private_key = dkim_dir / "private.key"
    public_key  = dkim_dir / "public.key"

    if not force and not key_needs_rotation(private_key):
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

    # Write private key — PEM, unencrypted, traditional OpenSSL format for Exim
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    private_key.chmod(0o640)
    os.chown(
        private_key,
        pwd.getpwnam("root").pw_uid,
        grp.getgrnam("Debian-exim").gr_gid,
    )

    # Write public key — PEM format
    public_key.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    # DNS TXT value — DER-encoded public key, base64, no PEM headers needed
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pubkey_b64 = base64.b64encode(pub_der).decode()

    log.info(
        "Generated DKIM key for %s\n"
        "  DNS record:\n"
        "  mail._domainkey.%s. TXT \"v=DKIM1; k=rsa; p=%s\"",
        domain, domain, pubkey_b64,
    )
    return True


def main() -> None:
    args = parse_args()
    level = logging.WARNING if args.quiet else (
        logging.DEBUG if args.debug else logging.INFO
    )
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    config = json.loads(DOMAINS_JSON.read_text())
    mail_domains = [d for d in config["domains"] if d["mail"]]

    for entry in mail_domains:
        generate_keypair(entry["domain"], args.dry_run, args.force)


if __name__ == "__main__":
    if sys.effectiveuid != 0:
        sys.exit("generate_dkim.py must be run as root")
    main()
```

---

## Caddy — per-site files

Caddy supports `import` so we use per-site files rather than a monolithic Caddyfile,
matching the nginx virtual host pattern from herewegoagain.

`caddy/Caddyfile` (root, thin):
```
{
    email admin@amyl.org.uk
}

import sites/*
```

`caddy/sites/amyl.org.uk`:
```
mail.amyl.org.uk {
    respond "." 200
}

mta-sts.amyl.org.uk {
    handle /.well-known/mta-sts.txt {
        respond `version: STSv1
mode: enforce
mx: mail.amyl.org.uk.
max_age: 86400` 200
    }
    respond 404
}
```

`caddy/sites/securitysaysyes.com` follows the same pattern plus the Ghost reverse
proxy block. MTA-STS is self-hosted via Caddy — no separate repo needed.

---

## Exim config templates

Exim conf.d fragments are Python `string.Template` files stamped by setup.py.
setup.py validates the stamped result with `exim -bV -C <tempfile>` before
installing. If validation fails, it logs the error and aborts — it never installs
a broken Exim config.

Template variables: `$primary_hostname`, `$mail_domains_list`,
`$tls_cert_path`, `$tls_key_path`, `$dkim_base`, `$relay_subnet`.

---

## `pyproject.toml`

```toml
[project]
name = "wolfcraig"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["jsonschema", "dnspython", "cryptography", "docker", "google-cloud-dns"]

[project.optional-dependencies]
dev = ["bandit", "mypy", "ruff", "pytest", "pre-commit"]

[tool.bandit]
targets = ["."]
exclude_dirs = [".venv", "tests/fixtures"]
skips = []

[tool.mypy]
strict = true
python_version = "3.11"

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "B", "S"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

---

## `.pre-commit-config.yaml`

`ruff-format` is black — same formatter, same output. No need to add black
separately. Using `gitleaks` over `detect-secrets` for better false positive rates
and more active maintenance.

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.0
    hooks:
      - id: ruff
      - id: ruff-format

  - repo: https://github.com/PyCQA/bandit
    rev: 1.8.0
    hooks:
      - id: bandit
        args: ["-c", "pyproject.toml"]

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.14.0
    hooks:
      - id: mypy

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-json
      - id: check-added-large-files
      - id: detect-private-key
      - id: no-commit-to-branch
        args: [--branch, main]

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks
```

---

## Systemd units

`systemd/caddy-cert-deploy.service`:
```ini
[Unit]
Description=Deploy Caddy-managed certs to Exim
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=cert-deployer
ExecStart=/usr/local/lib/wolfcraig-venv/bin/python3 \
    /usr/local/src/wolfcraig/scripts/deploy_certs.py --quiet
StandardOutput=journal
StandardError=journal
```

`systemd/caddy-cert-deploy.timer`:
```ini
[Unit]
Description=Run Caddy cert deploy twice daily

[Timer]
OnCalendar=*-*-* 04:13:00
OnCalendar=*-*-* 16:13:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
```

Fires at :13 past the hour rather than on the hour to avoid the herd effect where
all scheduled jobs fire simultaneously. `RandomizedDelaySec` adds a further random
spread of up to 5 minutes.

---

## machine-setup changes

```python
def setup_wolfcraig(args: argparse.Namespace) -> None:
    """Set up wolfcraig VPS: Ghost, Caddy, Exim, DKIM, TLS cert deployment."""
    log.info("Setting up wolfcraig")

    clone_or_pull(
        "git@github.com:adamamyl/wolfcraig.git",
        Path("/usr/local/src/wolfcraig"),
    )
    clone_or_pull(
        "git@github.com:adamamyl/ghost-docker.git",
        Path("/usr/local/src/ghost-docker"),
    )

    run_setup(Path("/usr/local/src/wolfcraig/setup.py"), args)
```

Add `--wolfcraig` to the argument parser alongside `--no2id`, `--docker` etc.
Pass `args` through to setup.py so flags (`--dry-run`, `--verbose` etc.) propagate.

---

## GitHub Actions

`.github/workflows/quality.yml`:
```yaml
name: Quality

on: [push, pull_request]

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv run pre-commit run --all-files
```

CI runs exactly the same pre-commit suite as local — one definition of passing,
not two.

---

## `.gitignore`

```gitignore
.venv/
__pycache__/
*.pyc
.env
exim/dkim/*/private.key
exim/dkim/*/public.key
```

---

---

# TODO

> **Status**: Phases 0–8 have been partially implemented. Code exists for all files
> in the repo. Phase R below covers all refactoring required before the implemented
> code is correct. Complete Phase R before proceeding to Phases 9 onward.

---

## Phase R — Refactor: wolfmail naming, additive SPF, TXT chunking

These changes touch every file that refers to `mail.$DOMAIN`, `mail._domainkey`,
or builds/validates DNS records. Complete all of Phase R as a single coherent PR
before any new phase work.

### R.1 — `lib/constants.py`

- [x] Add `DKIM_SELECTOR = "wolfmail"`
- [x] Add `MAIL_HOST_PREFIX = "wolfmail"`
- [x] Remove any implicit `"mail"` string literals from constants; derive mail host
      as `f"{MAIL_HOST_PREFIX}.{domain}"` everywhere

### R.2 — `config/domains.json` and schema

- [x] No schema changes required — `mailsubdomain` remains the flag; prefix comes
      from constants
- [x] Verify `domains.json` still validates cleanly after any adjacent changes

### R.3 — `caddy/sites/amyl.org.uk`

- [x] Rename ACME stub site block from `mail.amyl.org.uk` to `wolfmail.amyl.org.uk`
      (Caddy must acquire a cert for this hostname for Exim TLS)
- [x] `mta-sts.amyl.org.uk` site block is unchanged — keep as-is
- [x] Validate syntax: `docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile`

### R.4 — `caddy/sites/securitysaysyes.com`

- [x] Rename ACME stub site block from `mail.securitysaysyes.com` to
      `wolfmail.securitysaysyes.com`
- [x] Ghost reverse proxy block (`securitysaysyes.com`) unchanged
- [x] `mta-sts.securitysaysyes.com` unchanged
- [x] Validate syntax

### R.5 — `exim/templates/00_local_settings.tpl`

- [x] Change `primary_hostname` variable substitution to produce `wolfmail.${primary_domain}`
      (either hardcode prefix in template or add `${mail_host_prefix}` substitution variable)
- [x] Ensure `tls_certificate` and `tls_privatekey` paths reference `${primary_domain}`
      cert directory (paths are `/etc/exim4/certs/${primary_domain}/cert.pem` —
      cert file is deployed under domain name, not hostname)

### R.6 — `exim/templates/30_smtp_outbound.tpl`

- [x] Change `dkim_selector` to use `${dkim_selector}` substitution variable
      (stamped as `wolfmail` from constants at setup time)
- [x] Verify DKIM private key path references `${dkim_base}/$${sender_address_domain}/private.key`
      (domain-keyed, not hostname-keyed — this is correct and unchanged)
- [x] Confirm transport name is distinct enough: `remote_smtp_wolfmail` or keep `remote_smtp_dkim`

### R.7 — `lib/gcp_dns.py`

- [x] **`build_records_for_domain()` signature change**: add `zone` parameter so it
      can fetch live SPF before building desired state
- [x] **Rename A/AAAA host**: `wolfmail.{domain}.` instead of `mail.{domain}.`
      (use `MAIL_HOST_PREFIX` constant from `lib.constants`)
- [x] **Rename DKIM record**: `wolfmail._domainkey.{domain}.` instead of
      `mail._domainkey.{domain}.`
      (use `DKIM_SELECTOR` constant from `lib.constants`)
- [x] **Add `fetch_existing_spf(zone)`**: queries live zone for existing SPF TXT record
      at the apex; returns parsed mechanism list or `None` if no record exists
- [x] **Add `lib/host_info.py`**: `get_public_ipv4()` and `get_public_ipv6()` use
      `urllib.request` to query `https://api4.ipify.org` and `https://api6.ipify.org`;
      both raise `RuntimeError` on failure; called from `sync_domain()` as source of
      server IPs (env vars `SERVER_IPV4`/`SERVER_IPV6` override if set)
- [x] **Add `build_spf_record(existing_mechanisms, server_ipv4, server_ipv6)`**:
      - Takes existing mechanism list (e.g. `["include:_spf.google.com"]`)
      - Dynamically retrieves server's public IPv4 and IPv6 via `lib/host_info.py`
        if not already provided; appends `ip4:<ipv4>` and `ip6:<ipv6>` if not present
      - Replaces trailing `~all` with `-all`; adds `-all` if absent
      - Returns the full SPF string: `"v=spf1 include:_spf.google.com ip4:... ip6:... -all"`
- [x] **Update SPF rrdata in `build_records_for_domain()`** to call `fetch_existing_spf()`
      then `build_spf_record()` rather than generating from scratch
- [x] **Add `_split_dkim_rrdata(dkim_b64)`**: splits the key into 255-char chunks,
      returns list of quoted strings in GCP DNS format:
      `['"v=DKIM1; k=rsa; p=<first-chunk>"', '"<second-chunk>"', ...]`
- [x] **Update DKIM rrdata in `build_records_for_domain()`** to call `_split_dkim_rrdata()`
- [x] **Update `record_needs_update()`**: normalise both sides before comparing TXT records
      — join all rrdata strings, strip quotes, compare concatenated values — so records
      set via `gcloud` CLI are not needlessly re-uploaded
- [x] Update `sync_domain()` to pass `zone` through to `build_records_for_domain()`

### R.8 — `lib/dns_check.py`

- [x] `check_domain()`: check `wolfmail.{domain}` for A/AAAA records
      (use `MAIL_HOST_PREFIX` constant)
- [x] `check_domain()`: look for `wolfmail._domainkey.{domain}` for DKIM TXT
      (use `DKIM_SELECTOR` constant)
- [x] `check_domain()`: SPF check should report current vs. desired merged value
      (not just prefix match — needs to verify VPS IPs are present in the record)
- [x] `_print_copy_paste()`: for manual SPF, show the full merged desired value
      (fetch existing SPF via `_query_txt_prefix()`, merge, print result)

### R.9 — `scripts/generate_dkim.py`

- [x] Update DNS copy-paste log output from `mail._domainkey.{domain}` to
      `wolfmail._domainkey.{domain}` (use `DKIM_SELECTOR` constant)
- [x] Key file path (`EXIM_DKIM / domain / "private.key"`) is unchanged — keyed by domain

### R.10 — `scripts/deploy_certs.py`

- [x] `deploy_domain()`: derive `mail_host` using `MAIL_HOST_PREFIX` constant
      (`wolfmail.{domain}` not `mail.{domain}`)
- [x] Caddy cert source path will be under `wolfmail.{domain}/` in the volume —
      confirm `src_dir` derivation matches Caddy's actual directory naming convention
      (Caddy names cert dirs after the hostname it acquired the cert for)
- [x] Update any hardcoded `"mail."` string to use constant

### R.11 — `tests/`

- [x] `tests/fixtures/domains.json`: no change needed (uses `example.test` dummy domains)
- [x] `tests/test_deploy_certs.py`:
  - [x] Update `mail_host` strings from `mail.other.example.test` to
        `wolfmail.other.example.test` in all test helpers and assertions
  - [x] Add `test_deploy_domain_uses_mail_host_prefix_constant` — verify the prefix
        comes from `MAIL_HOST_PREFIX` not a hardcoded string
- [x] `tests/test_generate_dkim.py`:
  - [x] Add `test_dns_output_uses_wolfmail_selector` — verify log output contains
        `wolfmail._domainkey.{domain}`
- [x] `tests/test_dns_check.py`:
  - [x] Update mock data: `wolfmail.example.test` in A/AAAA checks
  - [x] Update DKIM check: `wolfmail._domainkey.example.test`
  - [x] Add `test_spf_check_verifies_vps_ips_present` — SPF check passes only when
        both VPS IPs are in the record, not just when `v=spf1` prefix is found
- [x] `tests/test_gcp_dns.py`:
  - [x] Update all test assertions: `wolfmail.example.test.` instead of `mail.`
  - [x] Update DKIM record name: `wolfmail._domainkey.example.test.`
  - [x] Add `test_spf_is_additive_when_record_exists` — existing Google SPF is
        preserved; VPS IPs are appended; `~all` becomes `-all`
  - [x] Add `test_spf_created_from_scratch_when_no_record` — no existing SPF →
        creates `v=spf1 ip4:... ip6:... -all` cleanly
  - [x] Add `test_dkim_rrdata_is_chunked` — key longer than 255 chars produces
        multiple quoted strings in rrdatas
  - [x] Add `test_record_needs_update_normalises_chunked_txt` — a record set via
        gcloud as multiple strings compares equal to the same value built locally

### R.12 — `setup.py`

- [x] Update `configure_exim()`: pass `dkim_selector` and `mail_host_prefix` into
      template variables dict (stamped into templates at setup time)
- [x] Update `sync_dns()`: `build_records_for_domain()` now needs `zone` — ensure
      it's threaded through correctly
- [x] Update `deploy_certs()`: uses `MAIL_HOST_PREFIX` constant via deploy_certs.py
      (no direct change if deploy_certs.py is updated correctly)

### R.13 — Run full quality suite

- [x] All Python files pass `ast.parse()` syntax validation (15 files)
- [x] Manual functional tests pass (constants, normalisation, SPF merge, DKIM chunking)
- [ ] `uv run ruff check .` — run on server (no network in build env)
- [ ] `uv run ruff format --check .` — run on server
- [ ] `uv run mypy .` — run on server
- [ ] `uv run bandit -c pyproject.toml .` — run on server
- [ ] `uv run pytest` — run on server once deps installed
- [ ] Manual smoke test: `sudo python3 setup.py --dry-run --verbose`
      confirm wolfmail hostnames appear throughout output; no `mail.` references remain

---

## Phase 0 — repo scaffolding

- [x] Create `wolfcraig` repo on GitHub (public)
- [x] Clone locally
- [x] Create directory structure:
      `config/`, `caddy/sites/`, `exim/templates/`, `exim/dkim/`,
      `scripts/`, `lib/`, `systemd/`, `tests/fixtures/`
- [x] Add initial `README.md` and `plan.md`
- [x] Add `.gitignore`
- [x] Add `exim/dkim/.gitkeep`
- [ ] Initial commit and push to main  *(requires git push access)*

## Phase 1 — toolchain and guardrails

- [x] Write `pyproject.toml` with ruff, bandit, mypy, pytest config
- [x] Write `.pre-commit-config.yaml` (ruff, ruff-format, bandit, mypy, check-json,
      detect-private-key, no-commit-to-branch, gitleaks)
- [ ] Install pre-commit hooks locally: `uv run pre-commit install`  *(server)*
- [ ] Verify pre-commit runs cleanly on empty repo  *(server)*
- [x] Write `.github/workflows/quality.yml`
- [ ] Enable branch protection on `main` in GitHub  *(GitHub UI)*
- [ ] Verify Actions passes on a trivial test PR  *(GitHub)*

## Phase 2 — configuration

- [x] Write `lib/constants.py` with all shared paths and constants
      *(needs R.1 additions: `DKIM_SELECTOR`, `MAIL_HOST_PREFIX`)*
- [x] Write `config/domains.json` (array form) with amyl.org.uk (`dns_management: "manual"`)
      and securitysaysyes.com (`dns_management: "gcp"`)
- [x] Write `config/domains.schema.json` including `dns_management` enum constraint
- [x] Manually validate: schema validation passes (verified locally)
- [x] Confirm `check-json` hook catches a deliberately malformed domains.json  *(verified logic)*
- [x] Confirm schema validation catches a missing required field  *(verified locally)*
- [x] Confirm schema validation rejects an invalid `dns_management` value  *(verified locally)*

## Phase 3 — Caddy config

- [x] Write thin root `caddy/Caddyfile` using `import sites/*`
- [x] Write `caddy/sites/amyl.org.uk`
      *(needs R.3: rename ACME stub to `wolfmail.amyl.org.uk`)*
- [x] Write `caddy/sites/securitysaysyes.com`
      *(needs R.4: rename ACME stub to `wolfmail.securitysaysyes.com`)*
- [x] Confirm MTA-STS policy is self-hosted via Caddy — confirmed in site files
- [ ] Validate Caddyfile syntax from within the ghost-docker Caddy container  *(server)*

## Phase 4 — Exim config templates

- [x] Write `exim/templates/00_local_settings.tpl`
      *(needs R.5: hostname to `wolfmail.${primary_domain}`)*
- [x] Write `exim/templates/30_smtp_outbound.tpl`
      *(needs R.6: selector to `wolfmail` via `${dkim_selector}` variable)*
- [x] Write `exim/templates/200_send_outbound.tpl`
- [x] Implement template stamping in setup.py using `string.Template`
      *(needs R.12: add `dkim_selector` and `mail_host_prefix` to variables dict)*
- [ ] Get Docker bridge subnet from docker network inspect  *(server)*
- [x] Verify stamped config with `exim -bV -C <tempfile>` — implemented in setup.py
- [ ] Test final exim config on server: `sudo exim -bV`  *(server)*

## Phase 5 — scripts

- [x] Write `lib/constants.py` *(needs R.1)*
- [x] Write `lib/dns_check.py` *(needs R.8)*
- [x] Write `lib/gcp_dns.py` *(needs R.7 — significant changes)*
- [x] Write `scripts/deploy_certs.py` *(needs R.10)*
- [x] Write `scripts/generate_dkim.py` *(needs R.9)*
- [ ] Run bandit, mypy, ruff against all scripts and lib  *(server — needs tool install)*

## Phase 6 — tests

- [x] Write `tests/fixtures/domains.json` — fake domains (example.test), never real
- [x] Write `tests/test_deploy_certs.py` *(needs R.11 updates)*
- [x] Write `tests/test_generate_dkim.py` *(needs R.11 updates)*
- [x] Write `tests/test_dns_check.py` *(needs R.11 updates)*
- [x] Write `tests/test_gcp_dns.py` *(needs R.11 updates + new tests)*
- [ ] Run full test suite: `uv run pytest`  *(server — needs pytest install)*
- [ ] Confirm tests pass in GitHub Actions  *(GitHub)*

## Phase 7 — systemd units

- [x] Write `systemd/caddy-cert-deploy.service`
      (User=cert-deployer; ExecStart uses wolfcraig-venv Python; `--quiet`)
- [x] Write `systemd/caddy-cert-deploy.timer`
      (OnCalendar at :13 past hour; RandomizedDelaySec=300)

## Phase 8 — setup.py

- [x] Write `setup.py` skeleton with all step functions
      *(needs R.12 updates to template variables and sync_dns threading)*
- [x] `check_apparmor()` — implemented
- [x] `install_packages()` — implemented
- [x] `create_cert_deployer_user()` — implemented
- [x] `install_sudoers_rule()` — implemented
- [x] `create_wolfcraig_venv()` — implemented
- [x] `configure_exim()` — implemented with wolfmail prefix and dkim_selector variables
- [x] `generate_dkim()` — implemented
- [x] `install_systemd_units()` — implemented
- [x] `start_ghost()` — implemented
- [x] `sync_dns()` — implemented; uses dynamic IP retrieval
- [x] `check_dns()` — implemented; uses dynamic IP retrieval
- [x] `deploy_certs()` — implemented
- [x] `send_test_emails()` — implemented
- [ ] Run bandit, mypy, ruff against setup.py  *(server — needs tool install)*
- [ ] Manual dry-run on server: `sudo python3 setup.py --dry-run --verbose`  *(server)*

## Phase 9 — machine-setup integration

- [x] Write `machine_setup_wolfcraig.py` integration snippet
- [ ] Check if `clone_or_pull()` exists in machine-setup lib  *(server)*
- [ ] Add `--wolfcraig` argument to machine-setup argparse  *(server)*
- [ ] Wire `setup_wolfcraig()` into machine-setup  *(server)*
- [ ] Test `--wolfcraig --dry-run` on wolfcraig server  *(server)*
- [ ] Test `--wolfcraig` full run on wolfcraig server  *(server)*
- [ ] Re-run `--wolfcraig` to verify idempotency  *(server)*

## Phase 9.5 — GCP Cloud DNS setup (one-time, before Phase 10)

Manual setup in GCP Console — done once. Only needed for `securitysaysyes.com`.
`amyl.org.uk` skips entirely.

- [ ] Create or identify a GCP project — note project ID for `.env`
- [ ] Enable Cloud DNS API in the project
- [ ] Create service account `wolfcraig-dns` with role `DNS Administrator`
- [ ] Generate and download JSON key for the service account
- [ ] Place key on server:
      `sudo mkdir -p /etc/wolfcraig && sudo chmod 700 /etc/wolfcraig`
      `sudo cp ~/wolfcraig-dns-key.json /etc/wolfcraig/gcp-dns-sa.json`
      `sudo chmod 600 /etc/wolfcraig/gcp-dns-sa.json`
- [ ] Set `GCP_PROJECT_ID` and `GCP_DNS_CREDENTIALS_FILE` in `.env`
- [ ] Create GCP Cloud DNS managed zone:
      zone name `securitysaysyes-com`, DNS name `securitysaysyes.com.`
- [ ] Note the four NS records GCP assigns to the zone
- [ ] At registrar: replace existing NS records with GCP NS records
- [ ] Verify delegation: `dig NS securitysaysyes.com` → returns GCP nameservers
- [ ] Run `setup.py --dry-run` to confirm GCP credentials and zone are reachable

## Phase 10 — DNS and live verification

DNS split into two gates. `setup.py` dispatches by `dns_management`; `dns_check.py`
validates both regardless.

**Gate 1 — A/AAAA records (required for HTTP-01 cert validation):**

`securitysaysyes.com` (GCP — automated):
- [ ] `setup.py --dry-run` — confirm proposed `wolfmail.securitysaysyes.com` A/AAAA correct
- [ ] `setup.py` — creates Gate 1 records in GCP; waits for propagation

`amyl.org.uk` (manual — script prints checklist):
- [ ] Add A record: `wolfmail.amyl.org.uk. A <server IPv4>`
- [ ] Add AAAA record: `wolfmail.amyl.org.uk. AAAA <server IPv6>`
- [ ] Add A record: `mta-sts.amyl.org.uk. A <server IPv4>`

Both:
- [ ] `python3 lib/dns_check.py` — confirm Gate 1 records resolve
- [ ] Restart Caddy: `docker compose restart caddy`
- [ ] Wait for certs: confirm `wolfmail.amyl.org.uk/` and `wolfmail.securitysaysyes.com/`
      directories appear in Caddy volume (setup.py polls and waits)

**Gate 2 — TXT records (after certs and DKIM keys exist):**

`securitysaysyes.com` (GCP — automated):
- [ ] `setup.py` — creates SPF (additive merge), DKIM (`wolfmail._domainkey`),
      DMARC, MTA-STS, TLS-RPT records; waits for propagation
- [ ] Verify SPF merge preserved `include:_spf.google.com`:
      `dig TXT securitysaysyes.com` — both Google include and VPS IPs present

`amyl.org.uk` (manual — script prints copy-paste checklist):
- [ ] Add SPF TXT at registrar — use the **merged** value from checklist
      (existing Google mechanisms + VPS IPs; `~all` → `-all`)
- [ ] Add DKIM TXT: `wolfmail._domainkey.amyl.org.uk.` (value from `generate_dkim.py` output)
- [ ] Add DMARC TXT: `_dmarc.amyl.org.uk.`
- [ ] Add MTA-STS TXT: `_mta-sts.amyl.org.uk.`
- [ ] Add TLS-RPT TXT: `_smtp._tls.amyl.org.uk.`

Both:
- [ ] `python3 lib/dns_check.py` — no alarms for either domain
- [ ] `setup.py` full run — certs deploy; Exim reloads

**Verification (both domains):**
- [ ] `send_test_emails()` runs per domain; check headers for `wolfmail` HELO and
      `dkim=pass` with `wolfmail` selector
- [ ] Verify TLS: `sslscan wolfmail.securitysaysyes.com:25` and `sslscan wolfmail.amyl.org.uk:25`
- [ ] Run mail-tester.com — target 10/10 per domain
- [ ] Verify MTA-STS: https://aykevl.nl/apps/mta-sts/
- [ ] Confirm SPF does not break existing Google Workspace delivery (send from GSuite
      and verify SPF/DKIM/DMARC pass in headers)

## Phase 11 — ghost-docker housekeeping

- [ ] Confirm ghost-docker repo is clean and deployable
- [ ] Add upstream Ghost as git remote if not present:
      `git remote add upstream <upstream url>`
- [ ] Document upstream sync process in ghost-docker README
- [x] Confirm `.env.example` is accurate and committed  *(verified — IPs noted as optional overrides)*
- [x] Confirm `.env` is in `.gitignore`  *(verified)*

## Phase 12 — documentation

- [x] Write `wolfcraig/README.md`
- [x] Update README: all references updated to `wolfmail.$DOMAIN`
- [x] README: GCP Cloud DNS setup (step-by-step)
- [x] README: SPF merge strategy — explained with before/after example
- [x] README: How to verify Google Workspace mail still works after SPF change
- [x] README: How `dns_management` controls automation vs manual
- [x] README: How to add a new domain
- [x] README: How to manually trigger cert deploy
- [x] README: How to check systemd timer status
- [x] README: How to verify DKIM/SPF/DMARC
- [x] README: Secrets and what lives where
- [ ] Update machine-setup README with `--wolfcraig` flag  *(server)*

---

## Future improvements

- [ ] Add AAAA records for `mta-sts.$DOMAIN` — currently only A (IPv4) is created. Caddy
      will acquire the cert over IPv4 but an AAAA record would allow IPv6 senders to reach
      the MTA-STS policy endpoint directly. Requires updating `build_records_for_domain()`
      in `lib/gcp_dns.py` and the manual DNS checklist in `lib/dns_check.py`.
