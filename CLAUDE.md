# wolfcraig

VPS configuration, scripts, and systemd units. Orchestrates Ghost (blog), Caddy (TLS/proxy),
and Exim (outbound mail) on a single Debian server. Run by machine-setup via `--wolfcraig`.

## What this repo does

- `server_setup.py` — root-level orchestration script run on the server (NOT a Python package)
- `lib/` — shared library code (constants, DNS check, GCP DNS, host IP detection)
- `scripts/` — standalone scripts: cert deploy, DKIM key generation
- `exim/templates/` — `string.Template` files stamped by `server_setup.py` at setup time
- `caddy/sites/` — Caddy site configs (included from `caddy/Caddyfile`)
- `config/domains.json` — per-domain config (mail, web, ghost, mailsubdomain, dns_management)
- `tests/` — offline unit tests (all external deps stubbed in `conftest.py`)

## Critical naming conventions

- Mail hostname prefix is `wolfmail` (constant `MAIL_HOST_PREFIX = "wolfmail"`)
- DKIM selector is `wolfmail` (constant `DKIM_SELECTOR = "wolfmail"`)
- This means: `wolfmail.amyl.org.uk`, `wolfmail._domainkey.securitysaysyes.com`, etc.
- Never hardcode the string `"mail."` — always use the constants from `lib/constants.py`
- Rationale: coexists cleanly with Google Workspace's `google._domainkey` and existing MX records

## DNS architecture

Two domains, different management:
- `securitysaysyes.com` — `dns_management: "gcp"` → GCP Cloud DNS, automated via `lib/gcp_dns.py`
- `amyl.org.uk` — `dns_management: "manual"` → prints copy-paste checklist

SPF is **additive**: read existing record, append VPS IPs, harden `~all` → `-all`.
Never replace the whole SPF record — must preserve `include:_spf.google.com`.

Server IPs are retrieved dynamically at runtime via `lib/host_info.py` (ipify.org).
`SERVER_IPV4` / `SERVER_IPV6` env vars override if set.

## Key design decisions

- `string.Template` for Exim config stamping (not Jinja2 — no extra dep)
- `dnspython` for DNS queries (not subprocess `dig`)
- GCP Cloud DNS client for zone management (not `gcloud` subprocess)
- DKIM TXT records chunked to 255-byte strings (RFC 4408)
- TXT record comparison normalises chunked strings before diffing (avoid spurious re-uploads)
- `tempfile.NamedTemporaryFile` for Exim config validation (not `/tmp/`)
- `server_setup.py` is NOT a pip-installable package — it does `sys.path.insert` itself

## Local dev setup

```bash
uv venv
uv pip install jsonschema dnspython cryptography docker google-cloud-dns google-auth \
    bandit mypy ruff pytest pre-commit types-jsonschema
./scripts/run_tests.sh
```

Tests run fully offline — `conftest.py` stubs `dns`, `docker`, and `google.cloud.dns`.

## Test runner

```bash
./scripts/run_tests.sh          # runs ruff, mypy, bandit, pytest; writes logs/run-TIMESTAMP.log
.venv/bin/ruff format .         # auto-fix formatting
.venv/bin/ruff check --fix .    # auto-fix safe lint issues
```

## Ruff config

`tests/*` ignores `S101` (assert is expected in pytest).
`S603`, `S607` ignored globally (subprocess with list args is intentional).

## Mypy

Strict mode. `google-cloud-dns` and `docker` have incomplete stubs — use
`# type: ignore[import-untyped]` on those import lines if needed, not a global weakening.

## File ownership rules

- `lib/constants.py` — single source of truth for all constants; no string literals elsewhere
- `lib/gcp_dns.py` — GCP DNS operations; `sync_domain()` fetches IPs internally
- `lib/dns_check.py` — validation; SPF check verifies both VPS IPs are present (not just prefix)
- `lib/host_info.py` — dynamic public IP retrieval via ipify.org
- `scripts/deploy_certs.py` — copies Caddy-managed certs into Exim; run as `cert-deployer` user
- `scripts/generate_dkim.py` — generates/rotates RSA-2048 DKIM keys (47-day cryptoperiod)
- `server_setup.py` — full orchestration; idempotent; safe to re-run

## Exim template variables

Templates in `exim/templates/*.tpl` use `string.Template` syntax (`${varname}`).
Variables stamped at setup time:
- `primary_hostname` — computed from `MAIL_HOST_PREFIX + "." + primary_domain`
- `primary_domain`, `relay_subnet`, `dkim_base`, `dkim_selector`, `mail_host_prefix`
- Exim's own `$$` escaping uses `$${sender_address_domain}` to produce `${sender_address_domain}`

## Domains in config/domains.json

- `amyl.org.uk` — mail + web, manual DNS
- `securitysaysyes.com` — mail + web + ghost, GCP DNS

## Things that only work on the server

- Exim config validation (`exim -bV`)
- Cert deployment (needs Docker volume + Exim running)
- GCP DNS sync (needs credentials at `/etc/wolfcraig/gcp-dns-sa.json`)
- Full `server_setup.py` run (needs root, apt, systemd)

## Plan

Full implementation plan and phase tracking: `plan.md`
All design rationale and phase history is in plan.md — read it before making structural changes.