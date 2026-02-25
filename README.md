# wolfcraig

Configuration, scripts, and systemd units for the wolfcraig VPS. Orchestrated by
[machine-setup](https://github.com/adamamyl/machine-setup) via `--wolfcraig`.

Runs Ghost (via [ghost-docker](https://github.com/adamamyl/ghost-docker)), Caddy
(TLS + reverse proxy), and Exim (outbound mail with DKIM/SPF/DMARC/MTA-STS).

Mail is sent from `wolfmail.<domain>` to coexist cleanly with existing Google
Workspace setups — the `wolfmail` prefix avoids collision with `google._domainkey`
and any existing MX/SPF configuration.

For implementation rationale and all design decisions, see [plan.md](./plan.md).

---

## Local development setup

This is how to get a working test environment on your laptop before pushing or
running anything on the server. All tools are managed through `uv` — no system
Python packages, no brew.

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your shell, or source the env file the installer prints:

```bash
source $HOME/.local/bin/env
```

Verify:

```bash
uv --version
```

### 2. Clone the repo (if you haven't already)

```bash
git clone https://github.com/adamamyl/wolfcraig.git ~/projects/wolfcraig
cd ~/projects/wolfcraig
```

### 3. Create the virtual environment

```bash
uv venv
```

This creates `.venv/` in the project root.

### 4. Install all dependencies including dev tools

```bash
uv sync --group dev
```

`uv sync` reads `pyproject.toml` and installs everything — runtime deps plus the dev
group (ruff, mypy, bandit, pytest, pre-commit) — into `.venv`. No editable install,
no build step, no setuptools involvement. First run pulls from PyPI; subsequent runs
use the cache.

### 5. Run the full test suite

```bash
./scripts/run_tests.sh
```

This runs ruff, mypy, bandit, and pytest in sequence and writes a timestamped log to
`logs/run-YYYYMMDD-HHMMSS.log`. The log file is what to attach when reporting test
results. Exit code is non-zero if any step fails.

Each step can also be run individually:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy lib/ scripts/ server_setup.py
.venv/bin/bandit -c pyproject.toml -r lib/ scripts/ server_setup.py -ll
.venv/bin/pytest -v
```

### Notes on mypy

`google-cloud-dns` and `docker` ship incomplete type stubs. mypy in strict mode will
report errors in code that imports them even though the runtime behaviour is correct.
The recommended approach is to add `# type: ignore[import-untyped]` on those import
lines when strict mode cannot be satisfied by a stub. This is preferable to weakening
the mypy config globally.

If mypy fails with unrelated errors in `lib/` or `scripts/`, fix those — they are real.
If it fails only on the third-party import lines, note it in the log and move on.

### Notes on pre-commit

Install the hooks once:

```bash
.venv/bin/pre-commit install
```

After that, hooks run automatically on `git commit`. To run against all files manually:

```bash
.venv/bin/pre-commit run --all-files
```

### What the tests cover (and what they don't)

The test suite runs entirely offline with no network access, no Docker daemon, and no
Exim binary. It stubs out `dns`, `docker`, and `google.cloud.dns` at the conftest level.

What is tested:
- DKIM key generation, rotation logic, cryptoperiod enforcement
- Cert comparison and deploy logic (filesystem mocks via `tmp_path`)
- DNS record construction — wolfmail host naming, DKIM selector, SPF merge
- GCP DNS record normalisation (chunked TXT comparison)
- SPF additive merge: existing Google mechanisms preserved, VPS IPs appended
- DKIM TXT chunking (RFC 4408 255-byte boundary)

What requires the live server:
- Exim config stamping and `exim -bV` validation
- Docker volume inspection for cert paths
- Actual DNS resolution against live zones
- GCP API calls

---

## First-time setup

```bash
sudo python3 /usr/local/src/machine-setup/setup_machine.py --wolfcraig --verbose
```

Or to see what would happen without making changes:

```bash
sudo python3 /usr/local/src/machine-setup/setup_machine.py --wolfcraig --dry-run --verbose
```

Before running, ensure `/usr/local/src/wolfcraig/.env` exists (copy from `.env.example`).
The script dynamically retrieves the server's public IPv4 and IPv6 addresses at runtime
using `lib/host_info.py` — you do not need to hardcode IPs.

---

## GCP Cloud DNS setup — `securitysaysyes.com`

`securitysaysyes.com` has `dns_management: "gcp"` in `config/domains.json`. All DNS
records for this domain are managed automatically. `amyl.org.uk` is `manual` and
produces a copy-paste checklist instead.

### One-time GCP setup (do this once before first `server_setup.py` run)

**1. Create or identify a GCP project**

```bash
gcloud projects list
# Note the project ID — goes in .env as GCP_PROJECT_ID
```

**2. Enable the Cloud DNS API**

```
GCP Console → APIs & Services → Enable APIs → Cloud DNS API
```

**3. Create a service account**

```
GCP Console → IAM & Admin → Service Accounts → Create
  Name: wolfcraig-dns
  Role: DNS Administrator
```

**4. Create and download a JSON key**

```
Service account → Keys → Add Key → Create new key → JSON → Download
```

**5. Place the key on the server**

```bash
sudo mkdir -p /etc/wolfcraig
sudo chmod 700 /etc/wolfcraig
sudo cp ~/wolfcraig-dns-key.json /etc/wolfcraig/gcp-dns-sa.json
sudo chmod 600 /etc/wolfcraig/gcp-dns-sa.json
sudo chown root:root /etc/wolfcraig/gcp-dns-sa.json
```

**6. Create the managed zone**

```
GCP Console → Network Services → Cloud DNS → Create zone
  Zone name: securitysaysyes-com
  DNS name: securitysaysyes.com.
  DNSSEC: off (can enable later)
```

**7. Delegate at the registrar**

After zone creation, GCP shows four NS records. At your registrar for
`securitysaysyes.com`, replace existing NS records with the four GCP ones:

```
securitysaysyes.com.  NS  ns-cloud-a1.googledomains.com.
securitysaysyes.com.  NS  ns-cloud-a2.googledomains.com.
securitysaysyes.com.  NS  ns-cloud-a3.googledomains.com.
securitysaysyes.com.  NS  ns-cloud-a4.googledomains.com.
```

(Exact NS names shown in GCP Console — copy from there.)

**8. Verify delegation**

```bash
dig NS securitysaysyes.com
# Should return GCP nameservers
```

**9. Populate `.env`**

```bash
cp .env.example .env
# Edit .env:
#   GCP_PROJECT_ID=your-project-id
#   GCP_DNS_CREDENTIALS_FILE=/etc/wolfcraig/gcp-dns-sa.json
# IPs are retrieved automatically — no need to set SERVER_IPV4/SERVER_IPV6
# unless you want to override the auto-detected values
```

---

## How DNS management works

`dns_management` in `config/domains.json` controls per-domain behaviour:

| Value | Behaviour |
|---|---|
| `"gcp"` | Script creates/updates all DNS records in GCP Cloud DNS automatically |
| `"manual"` | Script prints a copy-paste checklist; you add records at your registrar |

The server's public IPv4 and IPv6 addresses are retrieved dynamically at runtime
by querying `https://api4.ipify.org` and `https://api6.ipify.org`. Set
`SERVER_IPV4` / `SERVER_IPV6` in `.env` to override.

### SPF merge strategy

Both domains have existing Google Workspace SPF records. The script never replaces
the SPF record — it reads the existing value, appends the VPS IPs, and hardens
`~all` to `-all`:

```
# Before (Google Workspace only):
v=spf1 include:_spf.google.com ~all

# After wolfcraig (additive):
v=spf1 include:_spf.google.com ip4:<vps-ipv4> ip6:<vps-ipv6> -all
```

**To verify Google Workspace delivery still works after the SPF change:** send a
message from GSuite and inspect the `Authentication-Results` header — `spf=pass`
should appear with `smtp.mailfrom=@<domain>`.

### DKIM selector

Outbound mail is signed with the `wolfmail` selector (`wolfmail._domainkey.<domain>`),
distinct from `google._domainkey`. Both selectors can coexist in DNS without
interference.

---

## Adding a new domain

1. Add an entry to `config/domains.json` — set `mail`, `web`, `ghost`, `mailsubdomain`,
   and `dns_management` appropriately.
2. If `dns_management: "gcp"`, create the managed zone in GCP Console first (step 6 above).
3. Re-run `server_setup.py`:
   ```bash
   sudo python3 /usr/local/src/wolfcraig/server_setup.py --verbose
   ```
4. For manual domains, follow the printed DNS checklist exactly — the SPF value shown
   is the merged desired value (existing mechanisms + VPS IPs).

---

## Day-to-day operations

**Manually trigger cert deploy:**
```bash
sudo systemctl start caddy-cert-deploy.service
journalctl -u caddy-cert-deploy.service -f
```

**Check timer status:**
```bash
systemctl status caddy-cert-deploy.timer
systemctl list-timers caddy-cert-deploy.timer
```

**Validate DNS records:**
```bash
cd /usr/local/src/wolfcraig
python3 -c "
from lib import dns_check
from lib.host_info import get_public_ipv4, get_public_ipv6
import json
config = json.load(open('config/domains.json'))
ipv4, ipv6 = get_public_ipv4(), get_public_ipv6()
results = [dns_check.check_domain(d, ipv4, ipv6) for d in config['domains']]
dns_check.print_results(results)
"
```

**Force DKIM key rotation:**
```bash
sudo python3 /usr/local/src/wolfcraig/scripts/generate_dkim.py --force --verbose
```

After rotation, re-run `server_setup.py` to push the new `wolfmail._domainkey` TXT record
to GCP DNS (automated), or copy the printed value to your registrar (manual domains).

---

## Secrets and what lives where

| Secret | Location | Notes |
|---|---|---|
| GCP service account key | `/etc/wolfcraig/gcp-dns-sa.json` | `600 root:root`; never in repo |
| `.env` values | `/usr/local/src/wolfcraig/.env` | gitignored; copy from `.env.example` |
| DKIM private keys | `/etc/exim4/dkim/<domain>/private.key` | `640 root:Debian-exim`; gitignored |
| TLS certs | `/etc/exim4/certs/<domain>/cert.pem` | deployed from Caddy volume by timer |

---

## Verify DKIM/SPF/DMARC

After DNS propagates (allow up to 48h for full propagation):

**Check DNS records directly:**
```bash
dig TXT wolfmail._domainkey.securitysaysyes.com
dig TXT securitysaysyes.com           # SPF — verify VPS IPs and Google include both present
dig TXT _dmarc.securitysaysyes.com
dig TXT _mta-sts.securitysaysyes.com
dig A wolfmail.securitysaysyes.com    # should resolve to VPS IPv4
```

**Send a test email and inspect headers:**
```
Authentication-Results: ... dkim=pass header.s=wolfmail header.d=securitysaysyes.com
Authentication-Results: ... spf=pass smtp.mailfrom=...@securitysaysyes.com
Authentication-Results: ... dmarc=pass
```

**External tools:**
- **Mail tester**: https://www.mail-tester.com — target 10/10
- **MTA-STS**: https://aykevl.nl/apps/mta-sts/
- **TLS**: `sslscan wolfmail.securitysaysyes.com:25` and `sslscan wolfmail.amyl.org.uk:25`
