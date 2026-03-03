from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import NamedTuple

import dns.exception
import dns.resolver
from google.cloud import dns as gcp_dns  # type: ignore[import-untyped]
from google.oauth2 import service_account

from lib.constants import DKIM_SELECTOR, MAIL_HOST_PREFIX
from lib.host_info import get_public_ipv4, get_public_ipv6

log = logging.getLogger(__name__)

DEFAULT_TTL = 300


class DnsRecord(NamedTuple):
    name: str
    record_type: str
    ttl: int
    rrdatas: list[str]


def get_client(credentials_file: Path) -> gcp_dns.Client:
    project_id = os.environ["GCP_PROJECT_ID"]
    credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        str(credentials_file),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return gcp_dns.Client(project=project_id, credentials=credentials)


def get_zone(client: gcp_dns.Client, domain: str) -> gcp_dns.ManagedZone:
    zone_name = domain.rstrip(".").replace(".", "-")
    zone = client.zone(zone_name, dns_name=f"{domain.rstrip('.')}.")
    if not zone.exists():
        raise RuntimeError(
            f"GCP managed zone '{zone_name}' not found for domain '{domain}'. "
            f"Create it in the GCP Console first — see README.md."
        )
    return zone


def _normalise_txt(rrdatas: list[str]) -> str:
    # Sort rrdatas so comparison is order-insensitive (GCP may return them in any order).
    parts = sorted(r.replace('"', "").replace(" ", "") for r in rrdatas)
    return "".join(parts)


def record_needs_update(zone: gcp_dns.ManagedZone, record: DnsRecord) -> bool:
    for existing in zone.list_resource_record_sets():
        if existing.name == record.name and existing.record_type == record.record_type:
            if record.record_type == "TXT":
                if _normalise_txt(existing.rrdatas) == _normalise_txt(record.rrdatas):
                    log.debug("Record up to date: %s %s", record.record_type, record.name)
                    return False
            elif sorted(existing.rrdatas) == sorted(record.rrdatas):
                log.debug("Record up to date: %s %s", record.record_type, record.name)
                return False
            log.info(
                "Record differs: %s %s\n  current: %s\n  desired: %s",
                record.record_type,
                record.name,
                existing.rrdatas,
                record.rrdatas,
            )
            return True
    log.info("Record missing: %s %s", record.record_type, record.name)
    return True


def upsert_record(zone: gcp_dns.ManagedZone, record: DnsRecord, dry_run: bool) -> bool:
    if not record_needs_update(zone, record):
        return False

    if dry_run:
        log.info(
            "[dry-run] would upsert %s %s = %s",
            record.record_type,
            record.name,
            record.rrdatas,
        )
        return True

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
    deadline = time.time() + timeout_seconds
    log.info(
        "Waiting for %s %s to propagate (timeout %ds)...",
        record_type,
        name,
        timeout_seconds,
    )

    while time.time() < deadline:
        try:
            answers = dns.resolver.resolve(name, record_type)
            for rdata in answers:
                if record_type in ("A", "AAAA"):
                    value = str(rdata)
                else:
                    value = b"".join(rdata.strings).decode()
                if expected_value in value:
                    log.info("Propagated: %s %s", record_type, name)
                    return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException):
            pass

        log.debug("Not yet propagated, retrying in %ds...", poll_interval)
        time.sleep(poll_interval)

    log.warning("Timed out waiting for propagation of %s %s", record_type, name)
    return False


def fetch_existing_spf(zone: gcp_dns.ManagedZone, domain: str) -> list[str]:
    """Return SPF mechanisms from the apex TXT RRset, excluding qualifiers."""
    apex = f"{domain.rstrip('.')}."
    for existing in zone.list_resource_record_sets():
        if existing.name == apex and existing.record_type == "TXT":
            # Each rrdata is a separate TXT string — find only the SPF one.
            for rrdata in existing.rrdatas:
                raw = rrdata.strip('"')
                if raw.startswith("v=spf1"):
                    parts = raw.split()
                    return [p for p in parts if p not in ("v=spf1", "-all", "~all", "+all", "?all")]
    return []


def fetch_existing_non_spf_rrdatas(zone: gcp_dns.ManagedZone, domain: str) -> list[str]:
    """Return all non-SPF TXT rrdatas at the apex, to preserve alongside updated SPF."""
    apex = f"{domain.rstrip('.')}."
    for existing in zone.list_resource_record_sets():
        if existing.name == apex and existing.record_type == "TXT":
            return [r for r in existing.rrdatas if not r.strip('"').startswith("v=spf1")]
    return []


def build_spf_record(
    existing_mechanisms: list[str],
    server_ipv4: str,
    server_ipv6: str,
) -> str:
    mechanisms: list[str] = list(existing_mechanisms)
    if f"ip4:{server_ipv4}" not in mechanisms:
        mechanisms.append(f"ip4:{server_ipv4}")
    if server_ipv6 and f"ip6:{server_ipv6}" not in mechanisms:
        mechanisms.append(f"ip6:{server_ipv6}")
    parts = ["v=spf1"] + mechanisms + ["-all"]
    return " ".join(parts)


def _split_dkim_rrdata(dkim_b64: str) -> list[str]:
    header = f"v=DKIM1; k=rsa; p={dkim_b64}"
    chunks = [header[i : i + 255] for i in range(0, len(header), 255)]
    return [" ".join(f'"{chunk}"' for chunk in chunks)]


def build_records_for_domain(
    domain_config: dict[str, object],
    server_ipv4: str,
    server_ipv6: str,
    dkim_public_key_b64: str,
    mta_sts_id: str,
    zone: gcp_dns.ManagedZone,
) -> list[DnsRecord]:
    domain = str(domain_config["domain"])
    mailsubdomain = bool(domain_config.get("mailsubdomain", True))
    mail_host = f"{MAIL_HOST_PREFIX}.{domain}." if mailsubdomain else f"{domain}."

    records: list[DnsRecord] = [
        DnsRecord(mail_host, "A", DEFAULT_TTL, [server_ipv4]),
        DnsRecord(mail_host, "AAAA", DEFAULT_TTL, [server_ipv6]),
        DnsRecord(f"mta-sts.{domain}.", "A", DEFAULT_TTL, [server_ipv4]),
    ]

    if domain_config.get("mail"):
        existing_mechanisms = fetch_existing_spf(zone, domain)
        spf_value = build_spf_record(existing_mechanisms, server_ipv4, server_ipv6)
        # Preserve non-SPF TXT rrdatas at the apex (e.g. google-site-verification).
        non_spf = fetch_existing_non_spf_rrdatas(zone, domain)
        apex_txt = [f'"{spf_value}"'] + non_spf

        records += [
            DnsRecord(
                f"{domain}.",
                "TXT",
                DEFAULT_TTL,
                apex_txt,
            ),
            DnsRecord(
                f"{DKIM_SELECTOR}._domainkey.{domain}.",
                "TXT",
                DEFAULT_TTL,
                _split_dkim_rrdata(dkim_public_key_b64),
            ),
            DnsRecord(
                f"_dmarc.{domain}.",
                "TXT",
                DEFAULT_TTL,
                [f'"v=DMARC1; p=reject; pct=100; rua=mailto:dmarc@{domain}; adkim=s; aspf=r"'],
            ),
            DnsRecord(
                f"_mta-sts.{domain}.",
                "TXT",
                DEFAULT_TTL,
                [f'"v=STSv1; id={mta_sts_id}"'],
            ),
            DnsRecord(
                f"_smtp._tls.{domain}.",
                "TXT",
                DEFAULT_TTL,
                [f'"v=TLSRPTv1; rua=mailto:tls@{domain}"'],
            ),
        ]

    return records


def sync_domain(
    domain_config: dict[str, object],
    dkim_public_key_b64: str,
    mta_sts_id: str,
    credentials_file: Path,
    dry_run: bool,
    wait_propagation: bool = True,
) -> None:
    domain = str(domain_config["domain"])
    server_ipv4 = get_public_ipv4()
    server_ipv6 = get_public_ipv6()

    log.info("Server IPs: IPv4=%s IPv6=%s", server_ipv4, server_ipv6)

    client = get_client(credentials_file)
    zone = get_zone(client, domain)

    records = build_records_for_domain(
        domain_config, server_ipv4, server_ipv6, dkim_public_key_b64, mta_sts_id, zone
    )

    changed: list[DnsRecord] = []
    for record in records:
        if upsert_record(zone, record, dry_run):
            changed.append(record)

    if not changed:
        log.info("All DNS records for %s are up to date", domain)
        return

    if wait_propagation and not dry_run:
        for record in changed:
            wait_for_propagation(
                record.name.rstrip("."),
                record.record_type,
                record.rrdatas[0].strip('"'),
            )
