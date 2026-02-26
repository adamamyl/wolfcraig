from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Literal

import dns.exception
import dns.resolver

from lib.constants import DKIM_SELECTOR, MAIL_HOST_PREFIX

log = logging.getLogger(__name__)

Status = Literal["ok", "missing", "mismatch", "unknown"]


@dataclass
class RecordResult:
    domain: str
    record_type: str
    name: str
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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _spf_contains_ips(spf: str, server_ipv4: str, server_ipv6: str) -> bool:
    return f"ip4:{server_ipv4}" in spf and (not server_ipv6 or f"ip6:{server_ipv6}" in spf)


def _build_desired_spf(domain: str, server_ipv4: str, server_ipv6: str) -> str:
    existing = _query_txt_prefix(domain, "v=spf1")
    if existing is None:
        parts = ["v=spf1", f"ip4:{server_ipv4}"]
        if server_ipv6:
            parts.append(f"ip6:{server_ipv6}")
        parts.append("-all")
        return " ".join(parts)

    tokens = existing.split()
    mechanisms: list[str] = [
        t for t in tokens if t not in ("v=spf1", "-all", "~all", "+all", "?all")
    ]
    if f"ip4:{server_ipv4}" not in mechanisms:
        mechanisms.append(f"ip4:{server_ipv4}")
    if server_ipv6 and f"ip6:{server_ipv6}" not in mechanisms:
        mechanisms.append(f"ip6:{server_ipv6}")
    return " ".join(["v=spf1"] + mechanisms + ["-all"])


def check_domain(
    domain_config: dict[str, object],
    server_ipv4: str,
    server_ipv6: str,
    dkim_public_key_b64: str | None = None,
    mta_sts_id: str | None = None,
) -> DomainCheckResult:
    domain = str(domain_config["domain"])
    mailsubdomain = bool(domain_config.get("mailsubdomain", True))
    mail_host = f"{MAIL_HOST_PREFIX}.{domain}" if mailsubdomain else domain
    result = DomainCheckResult(domain=domain)

    for rtype, expected_ip in [("A", server_ipv4), ("AAAA", server_ipv6)]:
        actual = _query_address(mail_host, rtype)
        if actual is None:
            status: Status = "missing"
        elif actual == expected_ip:
            status = "ok"
        else:
            status = "mismatch"
        result.records.append(
            RecordResult(
                domain=domain,
                record_type=rtype,
                name=f"{mail_host}.",
                expected=expected_ip,
                actual=actual,
                status=status,
            )
        )

    if domain_config.get("mail"):
        desired_spf = _build_desired_spf(domain, server_ipv4, server_ipv6)
        spf_actual = _query_txt_prefix(domain, "v=spf1")
        if spf_actual is None:
            spf_status: Status = "missing"
        elif _spf_contains_ips(spf_actual, server_ipv4, server_ipv6):
            spf_status = "ok"
        else:
            spf_status = "mismatch"
        result.records.append(
            RecordResult(
                domain=domain,
                record_type="TXT",
                name=f"{domain}.",
                expected=desired_spf,
                actual=spf_actual,
                status=spf_status,
            )
        )

        dkim_name = f"{DKIM_SELECTOR}._domainkey.{domain}"
        dkim_actual = _query_txt_prefix(dkim_name, "v=DKIM1")
        dkim_expected = (
            f"v=DKIM1; k=rsa; p={dkim_public_key_b64}"
            if dkim_public_key_b64
            else "v=DKIM1; k=rsa; p=<key>"
        )
        result.records.append(
            RecordResult(
                domain=domain,
                record_type="TXT",
                name=f"{dkim_name}.",
                expected=dkim_expected,
                actual=dkim_actual,
                status=_txt_status(dkim_actual, "v=DKIM1"),
            )
        )

        sts_id = mta_sts_id or "<id>"
        for prefix, rname, expected_value in [
            (
                "v=DMARC1",
                f"_dmarc.{domain}",
                f"v=DMARC1; p=reject; pct=100; rua=mailto:dmarc@{domain}; adkim=s; aspf=s",
            ),
            ("v=STSv1", f"_mta-sts.{domain}", f"v=STSv1; id={sts_id}"),
            (
                "v=TLSRPTv1",
                f"_smtp._tls.{domain}",
                f"v=TLSRPTv1; rua=mailto:tls@{domain}",
            ),
        ]:
            actual_txt = _query_txt_prefix(rname, prefix)
            result.records.append(
                RecordResult(
                    domain=domain,
                    record_type="TXT",
                    name=f"{rname}.",
                    expected=expected_value,
                    actual=actual_txt,
                    status=_txt_status(actual_txt, prefix),
                )
            )

    return result


def print_results(results: list[DomainCheckResult]) -> None:
    print("\n" + "=" * 70)
    print("DNS VALIDATION RESULTS")
    print("=" * 70)

    for result in results:
        icon = "✓" if result.all_ok else "✗"
        print(f"\n{icon} {result.domain}")
        for rtype in ("A", "AAAA", "MX", "TXT"):
            for r in [x for x in result.records if x.record_type == rtype]:
                row_icon = "  ✓" if r.status == "ok" else "  ✗"
                print(f"{row_icon} {r.record_type:<6} {r.name}")
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
    for rtype in ("A", "AAAA", "TXT"):
        records = [r for result in results for r in result.missing if r.record_type == rtype]
        if records:
            print(f"\n; {rtype} records")
            for r in records:
                print(f"{r.name:<55} {r.record_type:<6} {r.expected}")


def _query_address(name: str, rtype: str) -> str | None:
    try:
        answers = dns.resolver.resolve(name, rtype)
        return str(answers[0])
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return None
    except dns.exception.DNSException as exc:
        log.warning("DNS query failed for %s %s: %s", rtype, name, exc)
        return None


def _query_txt_prefix(name: str, prefix: str) -> str | None:
    try:
        answers = dns.resolver.resolve(name, "TXT")
        for rdata in answers:
            value = b"".join(rdata.strings).decode()
            if value.startswith(prefix):
                return value
        return None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return None
    except dns.exception.DNSException as exc:
        log.warning("DNS TXT query failed for %s: %s", name, exc)
        return None


def _txt_status(actual: str | None, prefix: str) -> Status:
    if actual is None:
        return "missing"
    return "ok" if actual.startswith(prefix) else "mismatch"
