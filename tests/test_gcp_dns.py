from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.constants import DKIM_SELECTOR, MAIL_HOST_PREFIX
from lib.gcp_dns import (
    DnsRecord,
    _normalise_txt,
    _split_dkim_rrdata,
    build_records_for_domain,
    build_spf_record,
    fetch_existing_non_spf_rrdatas,
    fetch_existing_spf,
    record_needs_update,
    upsert_record,
    wait_for_propagation,
)


def _make_domain_config(
    domain: str = "example.test",
    mail: bool = True,
    mailsubdomain: bool = True,
    dns_management: str = "gcp",
) -> dict[str, object]:
    return {
        "domain": domain,
        "mail": mail,
        "web": True,
        "ghost": False,
        "mailsubdomain": mailsubdomain,
        "dns_management": dns_management,
    }


def _make_zone(existing_records: list[DnsRecord] | None = None) -> MagicMock:
    zone = MagicMock()
    mocks = []
    for r in existing_records or []:
        m = MagicMock()
        m.name = r.name
        m.record_type = r.record_type
        m.rrdatas = r.rrdatas
        mocks.append(m)
    zone.list_resource_record_sets.return_value = mocks
    zone.changes.return_value = MagicMock()
    return zone


def test_record_needs_update_when_missing() -> None:
    zone = _make_zone()
    record = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])
    assert record_needs_update(zone, record) is True


def test_record_needs_update_when_differs() -> None:
    existing = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["9.9.9.9"])
    zone = _make_zone([existing])
    desired = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])
    assert record_needs_update(zone, desired) is True


def test_record_no_update_when_identical() -> None:
    existing = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])
    zone = _make_zone([existing])
    desired = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])
    assert record_needs_update(zone, desired) is False


def test_record_needs_update_normalises_chunked_txt() -> None:
    chunked_existing = DnsRecord(
        f"{DKIM_SELECTOR}._domainkey.example.test.",
        "TXT",
        300,
        ['"v=DKIM1; k=rsa; p=AAAA" "BBBB"'],
    )
    zone = _make_zone([chunked_existing])
    single_desired = DnsRecord(
        f"{DKIM_SELECTOR}._domainkey.example.test.",
        "TXT",
        300,
        ['"v=DKIM1; k=rsa; p=AAAABBBB"'],
    )
    assert record_needs_update(zone, single_desired) is False


def test_upsert_dry_run_makes_no_api_calls() -> None:
    zone = _make_zone()
    record = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])
    result = upsert_record(zone, record, dry_run=True)
    assert result is True
    zone.changes.assert_not_called()


def test_upsert_deletes_existing_before_creating() -> None:
    existing = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["9.9.9.9"])
    zone = _make_zone([existing])
    changes = zone.changes.return_value
    desired = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])

    result = upsert_record(zone, desired, dry_run=False)

    assert result is True
    changes.delete_record_set.assert_called_once()
    changes.add_record_set.assert_called_once()
    changes.create.assert_called_once()


def test_upsert_skips_when_identical() -> None:
    existing = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])
    zone = _make_zone([existing])
    desired = DnsRecord(f"{MAIL_HOST_PREFIX}.example.test.", "A", 300, ["1.2.3.4"])

    result = upsert_record(zone, desired, dry_run=False)

    assert result is False
    zone.changes.assert_not_called()


def test_build_records_for_domain_includes_all_types() -> None:
    config = _make_domain_config()
    zone = _make_zone()

    records = build_records_for_domain(config, "1.2.3.4", "::1", "base64key==", "20240101", zone)

    types = {r.record_type for r in records}
    assert "A" in types
    assert "AAAA" in types
    assert "TXT" in types

    names = {r.name for r in records}
    assert f"{MAIL_HOST_PREFIX}.example.test." in names
    assert "mta-sts.example.test." in names
    assert f"{DKIM_SELECTOR}._domainkey.example.test." in names
    assert "_dmarc.example.test." in names
    assert "_mta-sts.example.test." in names
    assert "_smtp._tls.example.test." in names
    assert "mail._domainkey.example.test." not in names


def test_mta_sts_id_in_record_value() -> None:
    config = _make_domain_config()
    zone = _make_zone()
    records = build_records_for_domain(config, "1.2.3.4", "::1", "key==", "ts20240101", zone)

    mta_sts = [r for r in records if "_mta-sts" in r.name]
    assert len(mta_sts) == 1
    assert "ts20240101" in mta_sts[0].rrdatas[0]


def test_build_records_mailsubdomain_false() -> None:
    config = _make_domain_config(mailsubdomain=False)
    zone = _make_zone()
    records = build_records_for_domain(config, "1.2.3.4", "::1", "key==", "id", zone)
    a_records = [r for r in records if r.record_type == "A" and r.name == "example.test."]
    assert len(a_records) == 1


def test_spf_is_additive_when_record_exists() -> None:
    existing_spf = DnsRecord(
        "example.test.",
        "TXT",
        300,
        ['"v=spf1 include:_spf.google.com ~all"'],
    )
    zone = _make_zone([existing_spf])
    config = _make_domain_config()

    records = build_records_for_domain(config, "1.2.3.4", "::1", "key==", "id", zone)

    spf_records = [r for r in records if r.name == "example.test." and r.record_type == "TXT"]
    assert len(spf_records) == 1
    spf_value = spf_records[0].rrdatas[0]
    assert "include:_spf.google.com" in spf_value
    assert "ip4:1.2.3.4" in spf_value
    assert "ip6:::1" in spf_value
    assert "-all" in spf_value
    assert "~all" not in spf_value


def test_spf_created_from_scratch_when_no_record() -> None:
    zone = _make_zone()
    config = _make_domain_config()

    records = build_records_for_domain(config, "1.2.3.4", "::1", "key==", "id", zone)

    spf_records = [r for r in records if r.name == "example.test." and r.record_type == "TXT"]
    assert len(spf_records) == 1
    spf_value = spf_records[0].rrdatas[0]
    assert "v=spf1" in spf_value
    assert "ip4:1.2.3.4" in spf_value
    assert "ip6:::1" in spf_value
    assert spf_value.rstrip('"').endswith("-all")


def test_dkim_rrdata_is_chunked() -> None:
    long_key = "A" * 400
    rrdata = _split_dkim_rrdata(long_key)
    assert len(rrdata) == 1
    full = rrdata[0]
    assert '"' in full
    parts = [p.strip('"') for p in full.split('" "')]
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 255


def test_dkim_rrdata_short_key_single_chunk() -> None:
    short_key = "A" * 50
    rrdata = _split_dkim_rrdata(short_key)
    assert len(rrdata) == 1
    assert rrdata[0].startswith('"v=DKIM1')
    assert '"' in rrdata[0]


def test_normalise_txt_strips_quotes_and_spaces() -> None:
    chunked = ['"v=DKIM1; k=rsa; p=AAAA" "BBBB"']
    single = ['"v=DKIM1; k=rsa; p=AAAABBBB"']
    assert _normalise_txt(chunked) == _normalise_txt(single)


def test_fetch_existing_spf_returns_mechanisms() -> None:
    spf_record = MagicMock()
    spf_record.name = "example.test."
    spf_record.record_type = "TXT"
    spf_record.rrdatas = ['"v=spf1 include:_spf.google.com ~all"']
    zone = MagicMock()
    zone.list_resource_record_sets.return_value = [spf_record]

    mechanisms = fetch_existing_spf(zone, "example.test")
    assert "include:_spf.google.com" in mechanisms


def test_fetch_existing_spf_ignores_non_spf_rrdatas() -> None:
    """SPF parser must not bleed google-site-verification into SPF mechanisms."""
    spf_record = MagicMock()
    spf_record.name = "example.test."
    spf_record.record_type = "TXT"
    spf_record.rrdatas = [
        '"v=spf1 include:_spf.google.com ~all"',
        '"google-site-verification=abc123"',
    ]
    zone = MagicMock()
    zone.list_resource_record_sets.return_value = [spf_record]

    mechanisms = fetch_existing_spf(zone, "example.test")
    assert "include:_spf.google.com" in mechanisms
    assert not any("google-site-verification" in m for m in mechanisms)


def test_fetch_existing_spf_returns_empty_when_no_record() -> None:
    zone = _make_zone()
    mechanisms = fetch_existing_spf(zone, "example.test")
    assert mechanisms == []


def test_fetch_existing_non_spf_rrdatas_returns_others() -> None:
    spf_record = MagicMock()
    spf_record.name = "example.test."
    spf_record.record_type = "TXT"
    spf_record.rrdatas = [
        '"v=spf1 include:_spf.google.com ~all"',
        '"google-site-verification=abc123"',
    ]
    zone = MagicMock()
    zone.list_resource_record_sets.return_value = [spf_record]

    non_spf = fetch_existing_non_spf_rrdatas(zone, "example.test")
    assert non_spf == ['"google-site-verification=abc123"']


def test_apex_txt_preserves_non_spf_rrdatas() -> None:
    """google-site-verification must appear as a separate rrdata, not inside SPF."""
    existing = DnsRecord(
        "example.test.",
        "TXT",
        300,
        ['"v=spf1 include:_spf.google.com ~all"', '"google-site-verification=abc123"'],
    )
    zone = _make_zone([existing])
    config = _make_domain_config()

    records = build_records_for_domain(config, "1.2.3.4", "::1", "key==", "id", zone)

    apex = [r for r in records if r.name == "example.test." and r.record_type == "TXT"]
    assert len(apex) == 1
    rrdatas = apex[0].rrdatas
    spf_rdatas = [r for r in rrdatas if "v=spf1" in r]
    other_rdatas = [r for r in rrdatas if "google-site-verification" in r]
    assert len(spf_rdatas) == 1
    assert len(other_rdatas) == 1
    # google-site-verification must NOT appear inside the SPF rrdata
    assert "google-site-verification" not in spf_rdatas[0]


def test_dmarc_includes_pct100() -> None:
    config = _make_domain_config()
    zone = _make_zone()
    records = build_records_for_domain(config, "1.2.3.4", "::1", "key==", "id", zone)
    dmarc = [r for r in records if "_dmarc" in r.name]
    assert len(dmarc) == 1
    assert "pct=100" in dmarc[0].rrdatas[0]


def test_build_spf_record_appends_ips() -> None:
    mechanisms = ["include:_spf.google.com"]
    spf = build_spf_record(mechanisms, "1.2.3.4", "::1")
    assert spf == "v=spf1 include:_spf.google.com ip4:1.2.3.4 ip6:::1 -all"


def test_build_spf_record_does_not_duplicate_existing_ip() -> None:
    mechanisms = ["include:_spf.google.com", "ip4:1.2.3.4"]
    spf = build_spf_record(mechanisms, "1.2.3.4", "::1")
    assert spf.count("ip4:1.2.3.4") == 1


def test_build_spf_record_no_ipv6_when_empty() -> None:
    mechanisms = ["include:_spf.google.com"]
    spf = build_spf_record(mechanisms, "1.2.3.4", "")
    assert "ip6:" not in spf
    assert spf.endswith("-all")


def test_wait_for_propagation_returns_true_on_match() -> None:
    mock_rdata = MagicMock()
    mock_rdata.__str__ = MagicMock(return_value="1.2.3.4")
    mock_answers = MagicMock()
    mock_answers.__iter__ = MagicMock(return_value=iter([mock_rdata]))

    with patch("lib.gcp_dns.dns.resolver.resolve", return_value=mock_answers):
        result = wait_for_propagation(
            f"{MAIL_HOST_PREFIX}.example.test", "A", "1.2.3.4", timeout_seconds=30, poll_interval=1
        )

    assert result is True


def test_wait_for_propagation_returns_false_on_timeout() -> None:
    import dns.resolver

    with (
        patch("lib.gcp_dns.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN),
        patch("lib.gcp_dns.time.sleep"),
        patch("lib.gcp_dns.time.time", side_effect=[0] + [999] * 50),
    ):
        result = wait_for_propagation(
            f"{MAIL_HOST_PREFIX}.example.test", "A", "1.2.3.4", timeout_seconds=10, poll_interval=1
        )

    assert result is False


def test_get_zone_raises_if_not_found() -> None:
    from lib.gcp_dns import get_zone

    client = MagicMock()
    mock_zone = MagicMock()
    mock_zone.exists.return_value = False
    client.zone.return_value = mock_zone

    with pytest.raises(RuntimeError, match="not found"):
        get_zone(client, "example.test")
